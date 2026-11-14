from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from collections.abc import Iterable
from typing import Any, Callable, Protocol

from app.connectors.tushare.strategies import Dataset, RequestPlan, SyncScope, TushareMode, build_request_plans
from app.jobs.failure_retries import serialize_replay_request
from app.jobs.locks import JobLock, ProcessLocalJobLock


@dataclass(frozen=True)
class BackfillOptions:
    as_of: date
    market_years: int = 3
    market_start_override: date | None = None
    financial_start: date | None = None

    @property
    def market_start(self) -> date:
        if self.market_start_override is not None:
            return self.market_start_override
        try:
            return self.as_of.replace(year=self.as_of.year - self.market_years)
        except ValueError:
            return self.as_of.replace(year=self.as_of.year - self.market_years, day=28)

    @property
    def financial_window_start(self) -> date:
        return self.financial_start or self.market_start


FINANCIAL_AND_CORPORATE_ACTION_DATASETS = frozenset(
    {
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.FINANCIAL_EXPRESS,
        Dataset.DIVIDEND,
        Dataset.REPURCHASE,
        Dataset.SHARE_UNLOCK,
        Dataset.SHAREHOLDER_COUNT,
        Dataset.TOP_HOLDER,
        Dataset.BUSINESS_SEGMENT,
    }
)


def _closed_quarter_ends(start_date: date, end_date: date) -> tuple[date, ...]:
    quarters = (
        (3, 31),
        (6, 30),
        (9, 30),
        (12, 31),
    )
    return tuple(
        period
        for year in range(start_date.year, end_date.year + 1)
        for month, day in quarters
        if start_date <= (period := date(year, month, day)) <= end_date
    )


@dataclass(frozen=True)
class BackfillReport:
    planned: int
    completed: int
    failed: int
    results: tuple[Any, ...]
    failures: tuple["PlanFailure", ...] = ()
    job_run_id: str | None = None

    @property
    def rejected(self) -> int:
        return sum(int(getattr(result, "rejected_count", 0)) for result in self.results)


@dataclass(frozen=True)
class PlanFailure:
    dataset: Dataset
    request_key: str
    error_code: str
    message: str
    retryable: bool
    request_payload: dict[str, object] | None = None


class BackfillHistory(Protocol):
    def start(self, options: BackfillOptions, mode: TushareMode) -> str: ...

    def record_failure(self, run_id: str, failure: PlanFailure) -> None: ...

    def finish(self, run_id: str, report: BackfillReport) -> None: ...

    def fail(self, run_id: str, message: str) -> None: ...


class BackfillJob:
    def __init__(
        self,
        runner,
        *,
        lock: JobLock | None = None,
        history: BackfillHistory | None = None,
        sanitize_error: Callable[[str], str] | None = None,
    ) -> None:
        self._runner = runner
        self._lock = lock or ProcessLocalJobLock()
        self._history = history
        self._sanitize_error = sanitize_error or (lambda message: message)

    def plans(self, options: BackfillOptions, mode: TushareMode, datasets: Iterable[Dataset], symbols: Iterable[str] = ()) -> tuple[RequestPlan, ...]:
        requested_symbols = tuple(symbols)
        market_scope = SyncScope(
            symbols=requested_symbols,
            start_date=options.market_start,
            end_date=options.as_of,
        )
        financial_scope = SyncScope(
            symbols=requested_symbols,
            start_date=options.financial_window_start,
            end_date=options.as_of,
            periods=_closed_quarter_ends(options.financial_window_start, options.as_of),
        )
        return tuple(
            plan
            for dataset in datasets
            for plan in build_request_plans(
                dataset,
                mode,
                financial_scope if dataset in FINANCIAL_AND_CORPORATE_ACTION_DATASETS else market_scope,
            )
        )

    def run(self, options: BackfillOptions, mode: TushareMode, datasets: Iterable[Dataset], symbols: Iterable[str] = ()) -> BackfillReport:
        plans = self.plans(options, mode, datasets, symbols)
        key = f"backfill:{options.as_of.isoformat()}:{mode.value}"
        if not self._lock.try_acquire(key):
            return BackfillReport(len(plans), 0, len(plans), ())
        results: list[Any] = []
        failures: list[PlanFailure] = []
        job_run_id: str | None = None
        try:
            job_run_id = self._history.start(options, mode) if self._history else None
            for plan in plans:
                try:
                    results.append(self._runner(plan))
                except Exception as exc:
                    partial_result = getattr(exc, "partial_result", None)
                    if partial_result is not None:
                        results.append(partial_result)
                    replay_plan = getattr(exc, "request_plan", None) or plan
                    replay_mode = getattr(exc, "mode", None) or mode
                    failure = PlanFailure(
                        dataset=plan.dataset,
                        request_key=plan.cursor_key,
                        error_code=str(getattr(exc, "error_code", type(exc).__name__)),
                        message=self._sanitize_error(str(exc)),
                        retryable=bool(getattr(exc, "retryable", False)),
                        request_payload=serialize_replay_request(replay_plan, replay_mode),
                    )
                    failures.append(failure)
                    if self._history and job_run_id:
                        self._history.record_failure(job_run_id, failure)
            report = BackfillReport(
                len(plans),
                sum(bool(getattr(result, "completed", True)) for result in results),
                len(failures),
                tuple(results),
                tuple(failures),
                job_run_id,
            )
            if self._history and job_run_id:
                self._history.finish(job_run_id, report)
            return report
        except Exception as exc:
            if self._history and job_run_id:
                try:
                    self._history.fail(
                        job_run_id,
                        self._sanitize_error(str(exc)),
                    )
                except Exception:
                    pass
            raise
        finally:
            self._lock.release(key)
