from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from app.stocks.jobs.backfill import BackfillJob, BackfillOptions
from app.connectors.tushare.errors import TushareTransientError
from app.connectors.tushare.strategies import Dataset, TushareMode
from app.ingestion.pagination import PaginatedPlanError, PaginatedResult
from app.ingestion.pipeline import BatchResult


@dataclass(frozen=True)
class LegacyResult:
    row_count: int = 0
    rejected_count: int = 0


def make_batch_result(*, batch_id: str, received_count: int, rejected_count: int = 0) -> BatchResult:
    return BatchResult(
        batch_id=batch_id,
        dataset=Dataset.SECURITY,
        received_count=received_count,
        row_count=received_count - rejected_count,
        rejected_count=rejected_count,
    )


def test_backfill_uses_three_year_market_window() -> None:
    options = BackfillOptions(as_of=date(2026, 8, 17))

    assert options.market_start == date(2023, 8, 17)
    assert options.financial_start is None


def test_backfill_can_override_market_window_for_repair() -> None:
    options = BackfillOptions(
        as_of=date(2026, 8, 17),
        market_start_override=date(2026, 1, 1),
    )

    assert options.market_start == date(2026, 1, 1)


def test_backfill_uses_financial_start_for_financial_and_corporate_action_plans() -> None:
    plans = BackfillJob(lambda plan: plan).plans(
        BackfillOptions(
            as_of=date(2026, 8, 24),
            market_start_override=date(2023, 8, 24),
            financial_start=date(2024, 1, 1),
        ),
        TushareMode.STANDARD,
        (Dataset.DAILY, Dataset.INCOME, Dataset.DIVIDEND, Dataset.REPURCHASE),
        ("600519.SH",),
    )

    assert plans[0].params["start_date"] == "20230824"
    assert [plan.params["start_date"] for plan in plans[1:]] == [
        "20240101",
        "20240101",
        "20240101",
    ]


def test_backfill_vip_financial_plans_include_each_closed_quarter_only() -> None:
    plans = BackfillJob(lambda plan: plan).plans(
        BackfillOptions(as_of=date(2026, 8, 24), financial_start=date(2024, 1, 1)),
        TushareMode.VIP,
        (Dataset.INCOME,),
    )

    assert [plan.params["period"] for plan in plans] == [
        "20240331",
        "20240630",
        "20240930",
        "20241231",
        "20250331",
        "20250630",
        "20250930",
        "20251231",
        "20260331",
        "20260630",
    ]


def test_backfill_job_runs_plans_under_lock() -> None:
    seen = []
    job = BackfillJob(lambda plan: seen.append(plan) or plan.api_name)
    report = job.run(BackfillOptions(date(2026, 8, 17)), TushareMode.STANDARD, (Dataset.SECURITY,))
    assert report.completed == 1
    assert seen[0].api_name == "stock_basic"


def test_backfill_records_structured_provider_failure() -> None:
    class RecordingHistory:
        def __init__(self) -> None:
            self.failures = []
            self.finished = []

        def start(self, options, mode) -> str:
            return "run-1"

        def record_failure(self, run_id, failure) -> None:
            self.failures.append((run_id, failure))

        def finish(self, run_id, report) -> None:
            self.finished.append((run_id, report))

    def fail(_plan) -> None:
        raise TushareTransientError("temporary provider outage")

    history = RecordingHistory()
    job = BackfillJob(fail, history=history)

    report = job.run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    assert report.job_run_id == "run-1"
    assert report.completed == 0
    assert report.failed == 1
    assert report.failures[0].dataset is Dataset.SECURITY
    assert report.failures[0].request_key == "security:all"
    assert report.failures[0].error_code == "TushareTransientError"
    assert report.failures[0].retryable is True
    assert history.failures == [("run-1", report.failures[0])]
    assert history.finished == [("run-1", report)]


def test_backfill_counts_only_completed_paginated_results() -> None:
    paginated = PaginatedResult(
        dataset=Dataset.SECURITY,
        request_key="security:all",
        pages=2,
        request_count=2,
        resumed_from_offset=0,
        batches=(
            make_batch_result(batch_id="batch-1", received_count=3, rejected_count=1),
            make_batch_result(batch_id="batch-2", received_count=2),
        ),
        completed=True,
    )
    legacy = LegacyResult(row_count=4)
    results = iter((paginated, legacy))
    job = BackfillJob(lambda _plan: next(results))

    report = job.run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY, Dataset.TRADE_CALENDAR),
    )

    assert report.planned == 2
    assert report.completed == 2
    assert report.failed == 0
    assert report.results == (paginated, legacy)
    assert report.rejected == 1


def test_backfill_rejects_incomplete_paginated_result_without_failure() -> None:
    paginated = PaginatedResult(
        dataset=Dataset.SECURITY,
        request_key="security:all",
        pages=1,
        request_count=1,
        resumed_from_offset=0,
        batches=(make_batch_result(batch_id="batch-1", received_count=2, rejected_count=1),),
        completed=False,
    )
    job = BackfillJob(lambda _plan: paginated)

    report = job.run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    assert report.planned == 1
    assert report.completed == 0
    assert report.failed == 0
    assert report.results == (paginated,)
    assert report.rejected == 1
    assert report.failures == ()


def test_backfill_retains_partial_paginated_result_on_plan_error() -> None:
    partial = PaginatedResult(
        dataset=Dataset.SECURITY,
        request_key="security:all",
        pages=1,
        request_count=1,
        resumed_from_offset=0,
        batches=(make_batch_result(batch_id="batch-1", received_count=2),),
        completed=False,
    )

    def fail(_plan) -> None:
        raise PaginatedPlanError(
            "rate limit hit",
            partial_result=partial,
            error_code="rate_limit",
            retryable=True,
        )

    job = BackfillJob(fail)

    report = job.run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    assert report.planned == 1
    assert report.completed == 0
    assert report.failed == 1
    assert report.results == (partial,)
    assert report.rejected == 0
    assert report.failures[0].error_code == "rate_limit"
    assert report.failures[0].retryable is True


def test_backfill_releases_lock_when_history_start_fails() -> None:
    class FailingHistory:
        def start(self, options, mode) -> str:
            raise RuntimeError("history unavailable")

        def record_failure(self, run_id, failure) -> None:
            raise AssertionError("unreachable")

        def finish(self, run_id, report) -> None:
            raise AssertionError("unreachable")

    options = BackfillOptions(date(2026, 8, 17))
    failing_job = BackfillJob(lambda plan: plan, history=FailingHistory())

    with pytest.raises(RuntimeError, match="history unavailable"):
        failing_job.run(options, TushareMode.STANDARD, (Dataset.SECURITY,))

    retry = BackfillJob(lambda plan: plan).run(
        options,
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    assert retry.completed == 1


def test_backfill_marks_started_history_failed_when_finalization_raises() -> None:
    class FailingHistory:
        def __init__(self) -> None:
            self.aborted = []

        def start(self, options, mode) -> str:
            return "run-1"

        def record_failure(self, run_id, failure) -> None:
            raise AssertionError("unreachable")

        def finish(self, run_id, report) -> None:
            raise RuntimeError("history finalization failed")

        def fail(self, run_id, message) -> None:
            self.aborted.append((run_id, message))

    history = FailingHistory()
    job = BackfillJob(
        lambda plan: plan,
        history=history,
        sanitize_error=lambda message: message.replace("secret", "***"),
    )

    with pytest.raises(RuntimeError, match="history finalization failed"):
        job.run(
            BackfillOptions(date(2026, 8, 17)),
            TushareMode.STANDARD,
            (Dataset.SECURITY,),
        )

    assert history.aborted == [("run-1", "history finalization failed")]
