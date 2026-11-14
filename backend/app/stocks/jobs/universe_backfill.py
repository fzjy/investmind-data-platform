from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
import json
from typing import Callable, Literal, Protocol

from app.connectors.tushare.strategies import (
    BACKFILL_DATASETS,
    Dataset,
    TushareMode,
    VIP_API_NAMES,
)
from app.stocks.jobs.backfill import BackfillOptions, BackfillReport, PlanFailure
from app.jobs.build_and_publish import (
    BuildAndPublishOptions,
    BuildAndPublishReport,
)
from app.jobs.locks import JobLock, data_publication_lock_key
from app.stocks.universes.models import UniverseSelection


GLOBAL_DATASETS = (
    Dataset.SECURITY,
    Dataset.TRADE_CALENDAR,
    Dataset.CLASSIFICATION,
)
STOCK_DATASETS = tuple(
    dataset
    for dataset in sorted(BACKFILL_DATASETS, key=lambda item: item.value)
    # 业绩快报保留显式历史回填能力；日常任务与全池回填默认停采。
    if dataset not in GLOBAL_DATASETS and dataset != Dataset.FINANCIAL_EXPRESS
)
VIP_AGGREGATE_DATASETS = tuple(
    dataset for dataset in STOCK_DATASETS if dataset in VIP_API_NAMES
)


@dataclass(frozen=True)
class UniverseBackfillOptions:
    as_of: date
    mode: TushareMode
    selection: UniverseSelection
    market_years: int = 3
    financial_start: date | None = None
    batch_size: int = 10
    force: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.market_years <= 10:
            raise ValueError("market_years must be between 1 and 10")
        if not 1 <= self.batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        if self.financial_start is not None and self.financial_start > self.as_of:
            raise ValueError("financial_start must not follow as_of")


@dataclass(frozen=True)
class UniverseBackfillPlan:
    options: UniverseBackfillOptions
    business_key: str
    configuration_fingerprint: str
    market_start: date
    financial_start: date
    global_datasets: tuple[Dataset, ...]
    stock_datasets: tuple[Dataset, ...]
    symbol_batches: tuple[tuple[str, ...], ...]

    @property
    def run_once_datasets(self) -> tuple[Dataset, ...]:
        if self.options.mode is TushareMode.VIP:
            return (*self.global_datasets, *VIP_AGGREGATE_DATASETS)
        return self.global_datasets

    @property
    def batched_datasets(self) -> tuple[Dataset, ...]:
        if self.options.mode is TushareMode.VIP:
            return tuple(
                dataset
                for dataset in self.stock_datasets
                if dataset not in VIP_AGGREGATE_DATASETS
            )
        return self.stock_datasets

    def preflight_payload(self) -> dict[str, object]:
        selection = self.options.selection
        return {
            "event": "backfill-universe.preflight",
            "asOf": self.options.as_of.isoformat(),
            "marketStart": self.market_start.isoformat(),
            "financialStart": self.financial_start.isoformat(),
            "mode": self.options.mode.value,
            "universeProfile": selection.profile,
            "universeVersion": selection.version,
            "coverage": selection.coverage,
            "universeSize": len(selection.symbols),
            "batchSize": self.options.batch_size,
            "batchCount": len(self.symbol_batches),
        }


class UniverseBackfillPlanner:
    def build(self, options: UniverseBackfillOptions) -> UniverseBackfillPlan:
        market_start = self._market_start(options)
        financial_start = options.financial_start or market_start
        selection = options.selection
        business_key = ":".join(
            (
                options.as_of.isoformat(),
                options.mode.value,
                selection.profile,
                selection.version,
            )
        )
        fingerprint_payload = {
            "asOf": options.as_of.isoformat(),
            "mode": options.mode.value,
            "profile": selection.profile,
            "version": selection.version,
            "selectionFingerprint": selection.fingerprint,
            "marketStart": market_start.isoformat(),
            "financialStart": financial_start.isoformat(),
            "batchSize": options.batch_size,
        }
        encoded = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        symbol_batches = tuple(
            selection.symbols[index : index + options.batch_size]
            for index in range(0, len(selection.symbols), options.batch_size)
        )
        return UniverseBackfillPlan(
            options=options,
            business_key=business_key,
            configuration_fingerprint=hashlib.sha256(encoded).hexdigest(),
            market_start=market_start,
            financial_start=financial_start,
            global_datasets=GLOBAL_DATASETS,
            stock_datasets=STOCK_DATASETS,
            symbol_batches=symbol_batches,
        )

    @staticmethod
    def _market_start(options: UniverseBackfillOptions) -> date:
        try:
            return options.as_of.replace(
                year=options.as_of.year - options.market_years
            )
        except ValueError:
            return options.as_of.replace(
                year=options.as_of.year - options.market_years,
                day=28,
            )


@dataclass(frozen=True)
class UniverseBackfillCounts:
    provider_count: int = 0
    retained_count: int = 0
    filtered_count: int = 0
    row_count: int = 0
    rejected_count: int = 0
    request_count: int = 0

    def add_report(self, report: BackfillReport) -> UniverseBackfillCounts:
        results = tuple(report.results)
        return replace(
            self,
            provider_count=self.provider_count
            + sum(int(getattr(result, "received_count", 0)) for result in results),
            retained_count=self.retained_count
            + sum(int(getattr(result, "retained_count", 0)) for result in results),
            filtered_count=self.filtered_count
            + sum(int(getattr(result, "filtered_count", 0)) for result in results),
            row_count=self.row_count
            + sum(int(getattr(result, "row_count", 0)) for result in results),
            rejected_count=self.rejected_count
            + sum(int(getattr(result, "rejected_count", 0)) for result in results),
            request_count=self.request_count
            + sum(int(getattr(result, "request_count", 1)) for result in results),
        )

    def to_payload(self) -> dict[str, int]:
        return {
            "providerCount": self.provider_count,
            "retainedCount": self.retained_count,
            "filteredCount": self.filtered_count,
            "rowCount": self.row_count,
            "rejectedCount": self.rejected_count,
            "requestCount": self.request_count,
        }


@dataclass(frozen=True)
class UniverseBackfillReport:
    status: Literal["completed", "partial", "failed", "skipped", "locked"]
    job_run_id: str | None
    plan: UniverseBackfillPlan
    resumed: bool
    completed_batches: int
    counts: UniverseBackfillCounts
    publish_report: BuildAndPublishReport | None = None
    failures: tuple[PlanFailure, ...] = ()
    error: str | None = None

    def to_payload(self) -> dict[str, object]:
        publish = None
        if self.publish_report is not None:
            publish = {
                "status": self.publish_report.status,
                "version": self.publish_report.version,
                "products": list(self.publish_report.products),
                "quality": [
                    {
                        "name": result.name,
                        "severity": result.severity,
                        "passed": result.passed,
                        "details": dict(result.details),
                    }
                    for result in self.publish_report.quality
                ],
            }
            if self.publish_report.release_version is not None:
                publish["releaseVersion"] = self.publish_report.release_version
                publish["promotedProducts"] = list(
                    self.publish_report.promoted_products
                )
                publish["currentVersions"] = dict(
                    self.publish_report.current_versions
                )
        payload: dict[str, object] = {
            "status": self.status,
            "jobRunId": self.job_run_id,
            "resumed": self.resumed,
            "businessKey": self.plan.business_key,
            "universe": {
                "profile": self.plan.options.selection.profile,
                "version": self.plan.options.selection.version,
                "coverage": self.plan.options.selection.coverage,
                "size": len(self.plan.options.selection.symbols),
            },
            "completedBatches": self.completed_batches,
            "batchCount": len(self.plan.symbol_batches),
            "counts": self.counts.to_payload(),
            "publish": publish,
            "failures": [
                {
                    "dataset": failure.dataset.value,
                    "requestKey": failure.request_key,
                    "errorCode": failure.error_code,
                    "message": failure.message,
                    "retryable": failure.retryable,
                }
                for failure in self.failures
            ],
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


class UniverseBatchRunner(Protocol):
    def run(
        self,
        options: BackfillOptions,
        mode: TushareMode,
        datasets: Iterable[Dataset],
        symbols: Iterable[str] = (),
    ) -> BackfillReport: ...


class UniverseBatchRunnerFactory(Protocol):
    def __call__(self, cursor_job_name: str) -> UniverseBatchRunner: ...


class UniverseBackfillPublisher(Protocol):
    def run(self, options: BuildAndPublishOptions) -> BuildAndPublishReport: ...


class UniverseBackfillRunDecision(Protocol):
    action: Literal["start", "resume", "skip"]
    run_id: str
    cursor_job_name: str
    resumed: bool
    global_completed: bool
    next_batch_index: int
    counts: UniverseBackfillCounts


class UniverseBackfillHistory(Protocol):
    def begin(
        self,
        plan: UniverseBackfillPlan,
        now: datetime,
        *,
        force: bool,
    ) -> UniverseBackfillRunDecision: ...

    def checkpoint(
        self,
        run_id: str,
        *,
        next_batch_index: int,
        global_completed: bool,
        current_dataset: str | None,
        counts: UniverseBackfillCounts,
        now: datetime,
    ) -> None: ...

    def finish(
        self,
        run_id: str,
        status: Literal["completed", "partial", "failed"],
        now: datetime,
        *,
        details: Mapping[str, object],
    ) -> None: ...

    def fail(self, run_id: str, message: str, now: datetime) -> None: ...


class UniverseBackfillPlanningError(RuntimeError):
    """Raised when a universe backfill plan cannot be constructed."""


class UniverseBackfillJob:
    def __init__(
        self,
        *,
        planner: UniverseBackfillPlanner,
        history: UniverseBackfillHistory,
        batch_runner_factory: UniverseBatchRunnerFactory,
        publisher: UniverseBackfillPublisher,
        run_lock: JobLock,
        publication_lock: JobLock,
        clock: Callable[[], datetime],
        sanitize_error: Callable[[str], str],
    ) -> None:
        self._planner = planner
        self._history = history
        self._batch_runner_factory = batch_runner_factory
        self._publisher = publisher
        self._run_lock = run_lock
        self._publication_lock = publication_lock
        self._clock = clock
        self._sanitize_error = sanitize_error

    def plan(self, options: UniverseBackfillOptions) -> UniverseBackfillPlan:
        return self._planner.build(options)

    def run(self, options: UniverseBackfillOptions) -> UniverseBackfillReport:
        planning_error: UniverseBackfillPlanningError | None = None
        try:
            plan = self.plan(options)
        except Exception as exc:
            planning_error = UniverseBackfillPlanningError(
                self._sanitize_error(str(exc))
            )
        if planning_error is not None:
            raise planning_error from None
        lock_key = f"backfill-universe:{plan.business_key}"
        try:
            lock_acquired = self._run_lock.try_acquire(lock_key)
        except Exception as exc:
            return self._report(
                "failed",
                plan,
                error=self._sanitize_error(str(exc)),
            )
        if not lock_acquired:
            return self._report("locked", plan)

        try:
            report = self._run_acquired(plan, options)
        except BaseException:
            try:
                self._run_lock.release(lock_key)
            except Exception:
                pass
            raise

        try:
            self._run_lock.release(lock_key)
        except Exception as exc:
            if report.status in ("partial", "failed"):
                return report
            return replace(
                report,
                status="failed",
                error=self._sanitize_error(str(exc)),
            )
        return report

    def _run_acquired(
        self,
        plan: UniverseBackfillPlan,
        options: UniverseBackfillOptions,
    ) -> UniverseBackfillReport:
        decision: UniverseBackfillRunDecision | None = None
        counts = UniverseBackfillCounts()
        completed_batches = 0
        global_completed = False
        try:
            decision = self._history.begin(
                plan,
                self._clock(),
                force=options.force,
            )
            counts = decision.counts
            completed_batches = decision.next_batch_index
            global_completed = decision.global_completed
            if decision.action == "skip":
                return self._report(
                    "skipped",
                    plan,
                    decision=decision,
                    counts=counts,
                    completed_batches=completed_batches,
                )

            runner = self._batch_runner_factory(decision.cursor_job_name)
            batch_options = BackfillOptions(
                as_of=plan.options.as_of,
                market_start_override=plan.market_start,
                financial_start=plan.financial_start,
            )
            if not global_completed:
                batch_report = runner.run(
                    batch_options,
                    plan.options.mode,
                    plan.run_once_datasets,
                )
                counts = counts.add_report(batch_report)
                failures = self._sanitize_failures(batch_report.failures)
                if self._batch_failed(batch_report):
                    self._checkpoint_preserving_primary(
                        decision,
                        next_batch_index=completed_batches,
                        global_completed=global_completed,
                        counts=counts,
                    )
                    report = self._report(
                        "partial",
                        plan,
                        decision=decision,
                        counts=counts,
                        completed_batches=completed_batches,
                        failures=failures,
                    )
                    self._finish_preserving_primary(report)
                    return report
                self._history.checkpoint(
                    decision.run_id,
                    next_batch_index=completed_batches,
                    global_completed=True,
                    current_dataset=None,
                    counts=counts,
                    now=self._clock(),
                )
                global_completed = True

            for batch_index in range(
                decision.next_batch_index,
                len(plan.symbol_batches),
            ):
                batch_report = runner.run(
                    batch_options,
                    plan.options.mode,
                    plan.batched_datasets,
                    plan.symbol_batches[batch_index],
                )
                counts = counts.add_report(batch_report)
                failures = self._sanitize_failures(batch_report.failures)
                if self._batch_failed(batch_report):
                    self._checkpoint_preserving_primary(
                        decision,
                        next_batch_index=completed_batches,
                        global_completed=global_completed,
                        counts=counts,
                    )
                    report = self._report(
                        "partial",
                        plan,
                        decision=decision,
                        counts=counts,
                        completed_batches=completed_batches,
                        failures=failures,
                    )
                    self._finish_preserving_primary(report)
                    return report
                next_batch_index = batch_index + 1
                self._history.checkpoint(
                    decision.run_id,
                    next_batch_index=next_batch_index,
                    global_completed=global_completed,
                    current_dataset=None,
                    counts=counts,
                    now=self._clock(),
                )
                completed_batches = next_batch_index

            publication_key = data_publication_lock_key()
            if not self._publication_lock.try_acquire(publication_key):
                return self._report(
                    "locked",
                    plan,
                    decision=decision,
                    counts=counts,
                    completed_batches=completed_batches,
                )
            publish_report = self._publish_with_release_priority(
                publication_key,
                plan,
            )

            publish_report = self._sanitize_publish_report(publish_report)
            status: Literal["completed", "failed"] = (
                "completed"
                if publish_report.status == "completed"
                else "failed"
            )
            report = self._report(
                status,
                plan,
                decision=decision,
                counts=counts,
                completed_batches=completed_batches,
                publish_report=publish_report,
                error=publish_report.error if status == "failed" else None,
            )
            if status == "completed":
                self._history.finish(
                    decision.run_id,
                    status,
                    self._clock(),
                    details=report.to_payload(),
                )
            else:
                self._finish_preserving_primary(report)
            return report
        except Exception as exc:
            message = self._sanitize_error(str(exc))
            if decision is not None:
                try:
                    self._history.fail(decision.run_id, message, self._clock())
                except Exception:
                    pass
            return self._report(
                "failed",
                plan,
                decision=decision,
                counts=counts,
                completed_batches=completed_batches,
                error=message,
            )

    def _publish_with_release_priority(
        self,
        publication_key: str,
        plan: UniverseBackfillPlan,
    ) -> BuildAndPublishReport:
        try:
            publish_report = self._publisher.run(
                BuildAndPublishOptions(
                    plan.options.as_of,
                    plan.options.selection.symbols,
                )
            )
        except BaseException:
            try:
                self._publication_lock.release(publication_key)
            except Exception:
                pass
            raise

        try:
            self._publication_lock.release(publication_key)
        except Exception:
            if publish_report.status == "completed":
                raise
        return publish_report

    def _checkpoint_preserving_primary(
        self,
        decision: UniverseBackfillRunDecision,
        *,
        next_batch_index: int,
        global_completed: bool,
        counts: UniverseBackfillCounts,
    ) -> None:
        try:
            self._history.checkpoint(
                decision.run_id,
                next_batch_index=next_batch_index,
                global_completed=global_completed,
                current_dataset=None,
                counts=counts,
                now=self._clock(),
            )
        except Exception:
            pass

    @staticmethod
    def _batch_failed(report: BackfillReport) -> bool:
        return report.failed > 0 or bool(report.failures) or report.rejected > 0

    def _sanitize_failures(
        self,
        failures: Iterable[PlanFailure],
    ) -> tuple[PlanFailure, ...]:
        return tuple(
            replace(failure, message=self._sanitize_error(failure.message))
            for failure in failures
        )

    def _sanitize_publish_report(
        self,
        report: BuildAndPublishReport,
    ) -> BuildAndPublishReport:
        if report.error is None:
            return report
        return replace(report, error=self._sanitize_error(report.error))

    def _finish_preserving_primary(self, report: UniverseBackfillReport) -> None:
        if report.job_run_id is None or report.status not in (
            "partial",
            "failed",
        ):
            return
        try:
            self._history.finish(
                report.job_run_id,
                report.status,
                self._clock(),
                details=report.to_payload(),
            )
        except Exception:
            pass

    @staticmethod
    def _report(
        status: Literal["completed", "partial", "failed", "skipped", "locked"],
        plan: UniverseBackfillPlan,
        *,
        decision: UniverseBackfillRunDecision | None = None,
        counts: UniverseBackfillCounts = UniverseBackfillCounts(),
        completed_batches: int = 0,
        publish_report: BuildAndPublishReport | None = None,
        failures: tuple[PlanFailure, ...] = (),
        error: str | None = None,
    ) -> UniverseBackfillReport:
        return UniverseBackfillReport(
            status=status,
            job_run_id=None if decision is None else decision.run_id,
            plan=plan,
            resumed=False if decision is None else decision.resumed,
            completed_batches=completed_batches,
            counts=counts,
            publish_report=publish_report,
            failures=failures,
            error=error,
        )
