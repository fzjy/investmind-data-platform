from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
import traceback

import pytest

from app.connectors.tushare.strategies import (
    BACKFILL_DATASETS,
    Dataset,
    TushareMode,
)
from app.stocks.jobs.backfill import BackfillOptions, BackfillReport, PlanFailure
from app.jobs.build_and_publish import (
    BuildAndPublishOptions,
    BuildAndPublishReport,
)
from app.stocks.jobs.universe_backfill import (
    UniverseBackfillJob,
    UniverseBackfillCounts,
    UniverseBackfillOptions,
    UniverseBackfillPlan,
    UniverseBackfillPlanningError,
    UniverseBackfillPlanner,
    UniverseBackfillReport,
)
from app.marts.contracts import QualityResult
from app.stocks.universes.models import UniverseSelection


def selection(size: int = 50) -> UniverseSelection:
    symbols = tuple(f"{index:06d}.SZ" for index in range(1, size + 1))
    return UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=symbols,
        fingerprint="selection-fingerprint",
        warnings=(),
    )


def test_default_plan_is_three_years_and_five_batches() -> None:
    options = UniverseBackfillOptions(
        as_of=date(2026, 9, 12),
        mode=TushareMode.STANDARD,
        selection=selection(),
    )

    plan = UniverseBackfillPlanner().build(options)

    assert plan.market_start == date(2023, 9, 12)
    assert plan.financial_start == date(2023, 9, 12)
    assert tuple(len(batch) for batch in plan.symbol_batches) == (
        10,
        10,
        10,
        10,
        10,
    )
    assert (
        tuple(symbol for batch in plan.symbol_batches for symbol in batch)
        == options.selection.symbols
    )
    assert plan.business_key == (
        "2026-09-12:standard:representative-50:2026-09-04-v1"
    )


def test_leap_day_window_and_last_partial_batch_are_deterministic() -> None:
    options = UniverseBackfillOptions(
        as_of=date(2024, 2, 29),
        mode=TushareMode.VIP,
        selection=selection(21),
        market_years=3,
        batch_size=10,
    )

    plan = UniverseBackfillPlanner().build(options)

    assert plan.market_start == date(2021, 2, 28)
    assert tuple(len(batch) for batch in plan.symbol_batches) == (10, 10, 1)


def test_plan_separates_global_and_stock_datasets() -> None:
    plan = UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.STANDARD,
            selection=selection(2),
        )
    )

    assert plan.global_datasets == (
        Dataset.SECURITY,
        Dataset.TRADE_CALENDAR,
        Dataset.CLASSIFICATION,
    )
    assert set(plan.stock_datasets) == set(BACKFILL_DATASETS) - set(
        plan.global_datasets
    ) - {Dataset.FINANCIAL_EXPRESS}
    assert Dataset.DISCLOSURE_DATE not in plan.stock_datasets


def test_configuration_fingerprint_changes_with_resume_sensitive_fields() -> None:
    base = UniverseBackfillOptions(
        as_of=date(2026, 9, 12),
        mode=TushareMode.STANDARD,
        selection=selection(2),
    )

    fingerprints = {
        UniverseBackfillPlanner().build(base).configuration_fingerprint,
        UniverseBackfillPlanner()
        .build(replace(base, batch_size=1))
        .configuration_fingerprint,
        UniverseBackfillPlanner()
        .build(replace(base, market_years=2))
        .configuration_fingerprint,
    }

    assert len(fingerprints) == 3


def test_configuration_fingerprint_covers_identity_mode_and_windows() -> None:
    base = UniverseBackfillOptions(
        as_of=date(2026, 9, 12),
        mode=TushareMode.STANDARD,
        selection=selection(2),
    )
    custom_selection = replace(
        selection(2),
        profile="custom",
        version="custom-v1",
        coverage="custom",
        fingerprint="custom-fingerprint",
    )
    variants = (
        replace(base, as_of=date(2026, 9, 11)),
        replace(base, mode=TushareMode.VIP),
        replace(base, selection=custom_selection),
        replace(base, financial_start=date(2024, 1, 1)),
    )
    planner = UniverseBackfillPlanner()
    base_fingerprint = planner.build(base).configuration_fingerprint

    assert all(
        planner.build(options).configuration_fingerprint != base_fingerprint
        for options in variants
    )
    assert (
        planner.build(replace(base, force=True)).configuration_fingerprint
        == base_fingerprint
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"market_years": 0}, "market_years must be between 1 and 10"),
        ({"market_years": 11}, "market_years must be between 1 and 10"),
        ({"batch_size": 0}, "batch_size must be between 1 and 100"),
        ({"batch_size": 101}, "batch_size must be between 1 and 100"),
        (
            {"financial_start": date(2026, 9, 13)},
            "financial_start must not follow as_of",
        ),
    ],
)
def test_options_reject_invalid_windows_and_batch_sizes(
    changes: dict[str, object],
    message: str,
) -> None:
    values = {
        "as_of": date(2026, 9, 12),
        "mode": TushareMode.STANDARD,
        "selection": selection(2),
        **changes,
    }

    with pytest.raises(ValueError, match=message):
        UniverseBackfillOptions(**values)


def test_preflight_payload_is_stable_and_contains_no_request_secrets() -> None:
    plan = UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.STANDARD,
            selection=selection(),
        )
    )

    assert plan.preflight_payload() == {
        "event": "backfill-universe.preflight",
        "asOf": "2026-09-12",
        "marketStart": "2023-09-12",
        "financialStart": "2023-09-12",
        "mode": "standard",
        "universeProfile": "representative-50",
        "universeVersion": "2026-09-04-v1",
        "coverage": "representative",
        "universeSize": 50,
        "batchSize": 10,
        "batchCount": 5,
    }


@dataclass(frozen=True)
class DetailedResult:
    received_count: int
    retained_count: int
    filtered_count: int
    row_count: int
    rejected_count: int
    request_count: int


@dataclass(frozen=True)
class LegacyResult:
    row_count: int


def test_counts_accumulate_provider_filter_write_rejection_and_requests() -> None:
    report = BackfillReport(
        planned=2,
        completed=2,
        failed=0,
        results=(
            DetailedResult(
                received_count=8,
                retained_count=6,
                filtered_count=2,
                row_count=5,
                rejected_count=1,
                request_count=3,
            ),
            LegacyResult(row_count=2),
        ),
    )

    counts = UniverseBackfillCounts().add_report(report)

    assert counts.to_payload() == {
        "providerCount": 8,
        "retainedCount": 6,
        "filteredCount": 2,
        "rowCount": 7,
        "rejectedCount": 1,
        "requestCount": 4,
    }
    assert UniverseBackfillCounts().to_payload() == {
        "providerCount": 0,
        "retainedCount": 0,
        "filteredCount": 0,
        "rowCount": 0,
        "rejectedCount": 0,
        "requestCount": 0,
    }


def test_final_report_payload_is_stable_and_omits_sensitive_failure_fields() -> None:
    plan = UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.STANDARD,
            selection=selection(2),
        )
    )
    publish_report = BuildAndPublishReport(
        status="completed",
        job_run_id="publish-run",
        as_of=date(2026, 9, 12),
        version="snapshot-v1",
        symbols=selection(2).symbols,
        metric_count=3,
        risk_count=1,
        products=("market", "stocks"),
        quality=(
            QualityResult(
                name="source-completeness",
                severity="blocking",
                passed=True,
                details={"eligibleSecurityCount": 2},
            ),
        ),
        missing_data=(),
        release_version="snapshot-older",
        promoted_products=(),
        current_versions=(("market", "snapshot-v1"), ("stocks", "snapshot-v1")),
    )
    failure = PlanFailure(
        dataset=Dataset.DAILY,
        request_key="daily:000001.SZ",
        error_code="provider_error",
        message="sanitized failure",
        retryable=True,
        request_payload={
            "token": "secret-token",
            "url": "https://provider.example/api?token=secret-token",
            "params": {"api_key": "secret-token"},
            "traceback": "private stack",
        },
    )
    report = UniverseBackfillReport(
        status="partial",
        job_run_id="run-1",
        plan=plan,
        resumed=True,
        completed_batches=1,
        counts=UniverseBackfillCounts(
            provider_count=8,
            retained_count=6,
            filtered_count=2,
            row_count=5,
            rejected_count=1,
            request_count=3,
        ),
        publish_report=publish_report,
        failures=(failure,),
    )

    assert report.to_payload() == {
        "status": "partial",
        "jobRunId": "run-1",
        "resumed": True,
        "businessKey": (
            "2026-09-12:standard:representative-50:2026-09-04-v1"
        ),
        "universe": {
            "profile": "representative-50",
            "version": "2026-09-04-v1",
            "coverage": "representative",
            "size": 2,
        },
        "completedBatches": 1,
        "batchCount": 1,
        "counts": {
            "providerCount": 8,
            "retainedCount": 6,
            "filteredCount": 2,
            "rowCount": 5,
            "rejectedCount": 1,
            "requestCount": 3,
        },
        "publish": {
            "status": "completed",
            "version": "snapshot-v1",
            "releaseVersion": "snapshot-older",
            "promotedProducts": [],
            "currentVersions": {
                "market": "snapshot-v1",
                "stocks": "snapshot-v1",
            },
            "products": ["market", "stocks"],
            "quality": [
                {
                    "name": "source-completeness",
                    "severity": "blocking",
                    "passed": True,
                    "details": {"eligibleSecurityCount": 2},
                }
            ],
        },
        "failures": [
            {
                "dataset": "daily",
                "requestKey": "daily:000001.SZ",
                "errorCode": "provider_error",
                "message": "sanitized failure",
                "retryable": True,
            }
        ],
    }


NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)


@pytest.fixture
def plan() -> UniverseBackfillPlan:
    return UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.STANDARD,
            selection=selection(),
        )
    )


@dataclass(frozen=True)
class RecordingDecision:
    action: str = "start"
    run_id: str = "run-1"
    cursor_job_name: str = "backfill-universe:run-1"
    resumed: bool = False
    global_completed: bool = False
    next_batch_index: int = 0
    counts: UniverseBackfillCounts = UniverseBackfillCounts()


def decision(
    action: str,
    *,
    global_completed: bool = False,
    next_batch_index: int = 0,
    counts: UniverseBackfillCounts = UniverseBackfillCounts(),
) -> RecordingDecision:
    return RecordingDecision(
        action=action,
        resumed=action == "resume",
        global_completed=global_completed,
        next_batch_index=next_batch_index,
        counts=counts,
    )


class RecordingLock:
    def __init__(
        self,
        events: list[object],
        event_name: str,
        *,
        acquired: bool = True,
        acquire_error: BaseException | None = None,
        release_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._event_name = event_name
        self._acquired = acquired
        self._acquire_error = acquire_error
        self._release_error = release_error

    def try_acquire(self, value: str) -> bool:
        self._events.append((f"{self._event_name}.acquire", value))
        if self._acquire_error is not None:
            raise self._acquire_error
        return self._acquired

    def release(self, value: str) -> None:
        self._events.append((f"{self._event_name}.release", value))
        if self._release_error is not None:
            raise self._release_error


class RecordingHistory:
    def __init__(
        self,
        events: list[object],
        begin: RecordingDecision,
        *,
        checkpoint_error: BaseException | None = None,
        finish_error: BaseException | None = None,
        fail_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._begin = begin
        self._checkpoint_error = checkpoint_error
        self._finish_error = finish_error
        self._fail_error = fail_error
        self.checkpoints: list[
            tuple[int, bool, UniverseBackfillCounts]
        ] = []
        self.finished_details: list[dict[str, object]] = []

    def begin(
        self,
        plan: UniverseBackfillPlan,
        now: datetime,
        *,
        force: bool,
    ) -> RecordingDecision:
        assert now == NOW
        self._events.append(("history.begin", plan.business_key, force))
        return self._begin

    def checkpoint(
        self,
        run_id: str,
        *,
        next_batch_index: int,
        global_completed: bool,
        current_dataset: str | None,
        counts: UniverseBackfillCounts,
        now: datetime,
    ) -> None:
        assert run_id == "run-1"
        assert now == NOW
        self._events.append(
            ("history.checkpoint", global_completed, next_batch_index)
        )
        self.checkpoints.append((next_batch_index, global_completed, counts))
        if self._checkpoint_error is not None:
            raise self._checkpoint_error

    def finish(
        self,
        run_id: str,
        status: str,
        now: datetime,
        *,
        details: dict[str, object],
    ) -> None:
        assert run_id == "run-1"
        assert now == NOW
        self._events.append(("history.finish", status))
        self.finished_details.append(details)
        if self._finish_error is not None:
            raise self._finish_error

    def fail(self, run_id: str, message: str, now: datetime) -> None:
        assert run_id == "run-1"
        assert now == NOW
        self._events.append(("history.fail", message))
        if self._fail_error is not None:
            raise self._fail_error


class RecordingBatchRunner:
    def __init__(
        self,
        events: list[object],
        plan: UniverseBackfillPlan,
        *,
        fail_batch: int | None = None,
        batch_error: BaseException | None = None,
        batch_reports: dict[object, BackfillReport] | None = None,
    ) -> None:
        self._events = events
        self._plan = plan
        self._fail_batch = fail_batch
        self._batch_error = batch_error
        self._batch_reports = batch_reports or {}

    def run(
        self,
        options: BackfillOptions,
        mode: TushareMode,
        datasets: tuple[Dataset, ...],
        symbols: tuple[str, ...] = (),
    ) -> BackfillReport:
        assert options == BackfillOptions(
            as_of=self._plan.options.as_of,
            market_start_override=self._plan.market_start,
            financial_start=self._plan.financial_start,
        )
        assert mode is self._plan.options.mode
        if symbols:
            batch_index = self._plan.symbol_batches.index(tuple(symbols))
            self._events.append(
                ("batch.run", batch_index, tuple(datasets), tuple(symbols))
            )
        else:
            batch_index = None
            self._events.append(("batch.run", "global", tuple(datasets), ()))

        if self._batch_error is not None:
            raise self._batch_error
        report_key: object = "global" if batch_index is None else batch_index
        if report_key in self._batch_reports:
            return self._batch_reports[report_key]
        if self._fail_batch is not None and batch_index == self._fail_batch:
            failure = PlanFailure(
                dataset=Dataset.DAILY,
                request_key=f"daily:batch-{batch_index}",
                error_code="provider_error",
                message="batch failed",
                retryable=True,
            )
            return BackfillReport(1, 0, 1, (), (failure,))
        return BackfillReport(1, 1, 0, ())


class RecordingBatchRunnerFactory:
    def __init__(self, events: list[object], runner: RecordingBatchRunner) -> None:
        self._events = events
        self._runner = runner

    def __call__(self, cursor_job_name: str) -> RecordingBatchRunner:
        run_id = cursor_job_name.removeprefix("backfill-universe:")
        self._events.append(("runner.create", run_id))
        return self._runner


class RecordingPublisher:
    def __init__(
        self,
        events: list[object] | None = None,
        *,
        status: str = "completed",
        version: str | None = "snapshot-v1",
        error: BaseException | None = None,
        quality: tuple[QualityResult, ...] = (),
    ) -> None:
        self._events = events if events is not None else []
        self._status = status
        self._version = version
        self._error = error
        self._quality = quality
        self.calls: list[BuildAndPublishOptions] = []

    def run(self, options: BuildAndPublishOptions) -> BuildAndPublishReport:
        self.calls.append(options)
        self._events.append(("publish", options.as_of, options.symbols))
        if self._error is not None:
            raise self._error
        return BuildAndPublishReport(
            status=self._status,
            job_run_id="publish-run",
            as_of=options.as_of,
            version=self._version,
            symbols=options.symbols,
            metric_count=0,
            risk_count=0,
            products=("market",) if self._version else (),
            quality=self._quality,
            missing_data=(),
            error=None if self._status == "completed" else "publish failed",
        )


def make_job(
    events: list[object],
    plan: UniverseBackfillPlan,
    *,
    begin: RecordingDecision | None = None,
    fail_batch: int | None = None,
    batch_error: BaseException | None = None,
    batch_reports: dict[object, BackfillReport] | None = None,
    publisher: RecordingPublisher | None = None,
    planner: UniverseBackfillPlanner | None = None,
    history: RecordingHistory | None = None,
    lock_acquired: bool = True,
    publication_lock_acquired: bool = True,
    lock_acquire_error: BaseException | None = None,
    lock_release_error: BaseException | None = None,
    publication_lock_acquire_error: BaseException | None = None,
    publication_lock_release_error: BaseException | None = None,
    checkpoint_error: BaseException | None = None,
    finish_error: BaseException | None = None,
    fail_error: BaseException | None = None,
) -> UniverseBackfillJob:
    runner = RecordingBatchRunner(
        events,
        plan,
        fail_batch=fail_batch,
        batch_error=batch_error,
        batch_reports=batch_reports,
    )
    return UniverseBackfillJob(
        planner=planner or UniverseBackfillPlanner(),
        history=history
        or RecordingHistory(
            events,
            begin or decision("start"),
            checkpoint_error=checkpoint_error,
            finish_error=finish_error,
            fail_error=fail_error,
        ),
        batch_runner_factory=RecordingBatchRunnerFactory(events, runner),
        publisher=publisher or RecordingPublisher(events),
        run_lock=RecordingLock(
            events,
            "lock",
            acquired=lock_acquired,
            acquire_error=lock_acquire_error,
            release_error=lock_release_error,
        ),
        publication_lock=RecordingLock(
            events,
            "publication-lock",
            acquired=publication_lock_acquired,
            acquire_error=publication_lock_acquire_error,
            release_error=publication_lock_release_error,
        ),
        clock=lambda: NOW,
        sanitize_error=lambda message: message.replace("secret", "***"),
    )


def test_job_runs_global_then_batches_and_publishes_once(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    job = make_job(events, plan, begin=decision("start", next_batch_index=0))

    report = job.run(plan.options)

    expected: list[object] = [
        ("lock.acquire", f"backfill-universe:{plan.business_key}"),
        ("history.begin", plan.business_key, False),
        ("runner.create", "run-1"),
        ("batch.run", "global", plan.global_datasets, ()),
        ("history.checkpoint", True, 0),
    ]
    for index, batch in enumerate(plan.symbol_batches):
        expected.extend(
            (
                ("batch.run", index, plan.stock_datasets, batch),
                ("history.checkpoint", True, index + 1),
            )
        )
    expected.extend(
        (
            (
                "publication-lock.acquire",
                "data-publication",
            ),
            ("publish", plan.options.as_of, plan.options.selection.symbols),
            (
                "publication-lock.release",
                "data-publication",
            ),
            ("history.finish", "completed"),
            ("lock.release", f"backfill-universe:{plan.business_key}"),
        )
    )
    assert events == expected
    assert report.status == "completed"


def test_success_history_contains_real_publish_quality_payload(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    history = RecordingHistory(events, decision("start"))
    quality = QualityResult(
        name="source-completeness",
        severity="blocking",
        passed=True,
        details={"eligibleSecurityCount": 50},
    )

    report = make_job(
        events,
        plan,
        history=history,
        publisher=RecordingPublisher(events, quality=(quality,)),
    ).run(plan.options)

    assert report.status == "completed"
    assert history.finished_details[-1]["publish"] == {
        "status": "completed",
        "version": "snapshot-v1",
        "products": ["market"],
        "quality": [
            {
                "name": "source-completeness",
                "severity": "blocking",
                "passed": True,
                "details": {"eligibleSecurityCount": 50},
            }
        ],
    }


def test_resume_skips_global_and_completed_batches(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    job = make_job(
        events,
        plan,
        begin=decision("resume", global_completed=True, next_batch_index=3),
    )

    job.run(plan.options)

    assert not any(event[:2] == ("batch.run", "global") for event in events)
    assert [event[1] for event in events if event[0] == "batch.run"] == [3, 4]


def test_vip_aggregate_financial_datasets_run_once_across_multiple_batches() -> None:
    vip_plan = UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.VIP,
            selection=selection(3),
            financial_start=date(2025, 1, 1),
            batch_size=1,
        )
    )
    events: list[object] = []

    report = make_job(events, vip_plan).run(vip_plan.options)

    assert report.status == "completed"
    aggregate_datasets = (
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.BUSINESS_SEGMENT,
    )
    scheduled = [
        dataset
        for event in events
        if event[0] == "batch.run"
        for dataset in event[2]
    ]
    assert all(scheduled.count(dataset) == 1 for dataset in aggregate_datasets)
    stock_calls = [
        event
        for event in events
        if event[0] == "batch.run" and event[:2] != ("batch.run", "global")
    ]
    assert len(stock_calls) == 3
    assert all(
        not set(event[2]).intersection(aggregate_datasets)
        for event in stock_calls
    )


def test_vip_resume_after_once_phase_does_not_replay_aggregate_financial_datasets() -> None:
    vip_plan = UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.VIP,
            selection=selection(3),
            financial_start=date(2025, 1, 1),
            batch_size=1,
        )
    )
    events: list[object] = []

    report = make_job(
        events,
        vip_plan,
        begin=decision("resume", global_completed=True, next_batch_index=1),
    ).run(vip_plan.options)

    assert report.status == "completed"
    aggregate_datasets = {
        Dataset.INCOME,
        Dataset.BALANCE_SHEET,
        Dataset.CASHFLOW,
        Dataset.FINANCIAL_INDICATOR,
        Dataset.FINANCIAL_FORECAST,
        Dataset.BUSINESS_SEGMENT,
    }
    assert [event[1] for event in events if event[0] == "batch.run"] == [1, 2]
    assert all(
        not set(event[2]).intersection(aggregate_datasets)
        for event in events
        if event[0] == "batch.run"
    )


def test_skip_returns_persisted_progress_without_runner_or_publish(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    persisted = UniverseBackfillCounts(
        provider_count=10,
        retained_count=9,
        filtered_count=1,
        row_count=8,
        rejected_count=1,
        request_count=2,
    )

    report = make_job(
        events,
        plan,
        begin=decision(
            "skip",
            global_completed=True,
            next_batch_index=len(plan.symbol_batches),
            counts=persisted,
        ),
    ).run(plan.options)

    assert report.status == "skipped"
    assert report.counts == persisted
    assert report.completed_batches == len(plan.symbol_batches)
    assert not any(event[0] in ("runner.create", "publish") for event in events)
    assert events[-1] == (
        "lock.release",
        f"backfill-universe:{plan.business_key}",
    )


def test_resume_adds_each_count_to_persisted_totals(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    persisted = UniverseBackfillCounts(
        provider_count=11,
        retained_count=10,
        filtered_count=1,
        row_count=9,
        rejected_count=0,
        request_count=4,
    )
    final_batch = BackfillReport(
        planned=1,
        completed=1,
        failed=0,
        results=(
            DetailedResult(
                received_count=8,
                retained_count=6,
                filtered_count=2,
                row_count=5,
                rejected_count=0,
                request_count=3,
            ),
        ),
    )

    report = make_job(
        events,
        plan,
        begin=decision(
            "resume",
            global_completed=True,
            next_batch_index=4,
            counts=persisted,
        ),
        batch_reports={4: final_batch},
    ).run(plan.options)

    assert report.counts.to_payload() == {
        "providerCount": 19,
        "retainedCount": 16,
        "filteredCount": 3,
        "rowCount": 14,
        "rejectedCount": 0,
        "requestCount": 7,
    }


def test_force_is_forwarded_only_to_history_begin(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(events, plan).run(replace(plan.options, force=True))

    assert report.status == "completed"
    assert ("history.begin", plan.business_key, True) in events


def test_failed_batch_stops_before_publish_and_keeps_checkpoint(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    job = make_job(events, plan, fail_batch=2)

    report = job.run(plan.options)

    assert report.status == "partial"
    assert ("history.checkpoint", True, 2) in events
    assert ("history.checkpoint", True, 3) not in events
    assert not any(event[0] == "publish" for event in events)
    assert events[-1][0] == "lock.release"


def test_failed_publish_is_terminal_and_preserves_previous_snapshot(
    plan: UniverseBackfillPlan,
) -> None:
    publisher = RecordingPublisher(status="failed", version=None)

    report = make_job([], plan, publisher=publisher).run(plan.options)

    assert report.status == "failed"
    assert publisher.calls == [
        BuildAndPublishOptions(plan.options.as_of, plan.options.selection.symbols)
    ]


def test_lock_conflict_returns_without_history_or_runner(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(events, plan, lock_acquired=False).run(plan.options)

    assert report.status == "locked"
    assert events == [("lock.acquire", f"backfill-universe:{plan.business_key}")]


def test_publication_lock_conflict_keeps_completed_checkpoint_without_publish(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(
        events,
        plan,
        publication_lock_acquired=False,
    ).run(plan.options)

    assert report.status == "locked"
    assert (
        "history.checkpoint",
        True,
        len(plan.symbol_batches),
    ) in events
    assert not any(event[0] == "publish" for event in events)
    assert not any(event[0] in ("history.finish", "history.fail") for event in events)


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_process_control_signal_propagates_and_releases_lock(
    plan: UniverseBackfillPlan,
    signal: type[BaseException],
) -> None:
    events: list[object] = []
    job = make_job(events, plan, batch_error=signal())

    with pytest.raises(signal):
        job.run(plan.options)

    assert events[-1][0] == "lock.release"


def test_checkpoint_failure_does_not_advance_current_batch(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(
        events,
        plan,
        begin=decision("resume", global_completed=True, next_batch_index=2),
        checkpoint_error=RuntimeError("checkpoint token=secret"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.completed_batches == 2
    assert report.error == "checkpoint token=***"
    assert [event[1] for event in events if event[0] == "batch.run"] == [2]
    assert ("history.fail", "checkpoint token=***") in events


def test_batch_exception_is_sanitized_and_secondary_history_failure_is_ignored(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(
        events,
        plan,
        batch_error=RuntimeError("provider token=secret"),
        fail_error=RuntimeError("secondary history failure"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.error == "provider token=***"
    assert ("history.fail", "provider token=***") in events
    assert events[-1][0] == "lock.release"


def test_failed_batch_is_sanitized_and_survives_terminal_history_failure(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    failed = BackfillReport(
        planned=1,
        completed=0,
        failed=1,
        results=(),
        failures=(
            PlanFailure(
                dataset=Dataset.DAILY,
                request_key="daily:batch-0",
                error_code="provider_error",
                message="provider token=secret",
                retryable=True,
            ),
        ),
    )

    report = make_job(
        events,
        plan,
        batch_reports={0: failed},
        finish_error=RuntimeError("secondary terminal failure"),
    ).run(plan.options)

    assert report.status == "partial"
    assert report.failures[0].message == "provider token=***"
    assert ("history.finish", "partial") in events
    assert events[-1][0] == "lock.release"


def test_rejected_rows_stop_before_publish_and_keep_independent_counts(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    rejected = BackfillReport(
        planned=1,
        completed=1,
        failed=0,
        results=(
            DetailedResult(
                received_count=9,
                retained_count=7,
                filtered_count=2,
                row_count=5,
                rejected_count=2,
                request_count=3,
            ),
        ),
    )

    report = make_job(
        events,
        plan,
        batch_reports={0: rejected},
    ).run(plan.options)

    assert report.status == "partial"
    assert report.counts.to_payload() == {
        "providerCount": 9,
        "retainedCount": 7,
        "filteredCount": 2,
        "rowCount": 5,
        "rejectedCount": 2,
        "requestCount": 3,
    }
    assert not any(event[0] == "publish" for event in events)


def test_publisher_exception_is_sanitized_and_releases_both_locks(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    publisher = RecordingPublisher(
        events,
        error=RuntimeError("publish token=secret"),
    )

    report = make_job(
        events,
        plan,
        publisher=publisher,
        fail_error=RuntimeError("secondary history failure"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.error == "publish token=***"
    assert ("history.fail", "publish token=***") in events
    event_names = [event[0] for event in events]
    assert event_names.index("publication-lock.release") < event_names.index(
        "history.fail"
    )
    assert events[-1][0] == "lock.release"


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_publisher_control_signal_propagates_and_releases_both_locks(
    plan: UniverseBackfillPlan,
    signal: type[BaseException],
) -> None:
    events: list[object] = []
    publisher = RecordingPublisher(events, error=signal())

    with pytest.raises(signal):
        make_job(events, plan, publisher=publisher).run(plan.options)

    assert events[-2][0] == "publication-lock.release"
    assert events[-1][0] == "lock.release"


class RaisingPlanner(UniverseBackfillPlanner):
    def build(self, options: UniverseBackfillOptions) -> UniverseBackfillPlan:
        raise RuntimeError("planner token=secret")


def test_planner_failure_raises_sanitized_domain_error_without_side_effects(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    with pytest.raises(
        UniverseBackfillPlanningError,
        match=r"planner token=\*\*\*",
    ):
        make_job(events, plan, planner=RaisingPlanner()).run(plan.options)

    assert events == []


def test_planner_failure_does_not_expose_original_exception_chain(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    with pytest.raises(UniverseBackfillPlanningError) as captured:
        make_job(events, plan, planner=RaisingPlanner()).run(plan.options)

    error = captured.value
    formatted = "".join(traceback.format_exception(error))
    assert "planner token=secret" not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


def test_run_lock_acquisition_error_returns_sanitized_failed_report_without_release(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(
        events,
        plan,
        lock_acquire_error=RuntimeError("lock token=secret"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.error == "lock token=***"
    assert events == [("lock.acquire", f"backfill-universe:{plan.business_key}")]


def test_publication_lock_acquisition_error_does_not_release_unacquired_lock(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(
        events,
        plan,
        publication_lock_acquire_error=RuntimeError("publication token=secret"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.error == "publication token=***"
    assert not any(event[0] == "publication-lock.release" for event in events)
    assert events[-1][0] == "lock.release"


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_control_signal_survives_both_lock_release_failures(
    plan: UniverseBackfillPlan,
    signal: type[BaseException],
) -> None:
    events: list[object] = []
    publisher = RecordingPublisher(events, error=signal())

    with pytest.raises(signal):
        make_job(
            events,
            plan,
            publisher=publisher,
            publication_lock_release_error=RuntimeError("publication release"),
            lock_release_error=RuntimeError("run release"),
        ).run(plan.options)

    assert [event[0] for event in events[-2:]] == [
        "publication-lock.release",
        "lock.release",
    ]


def test_primary_business_error_survives_run_lock_release_failure(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []

    report = make_job(
        events,
        plan,
        batch_error=RuntimeError("provider token=secret"),
        lock_release_error=RuntimeError("release token=secret"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.error == "provider token=***"


def test_publisher_error_survives_publication_lock_release_failure(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    publisher = RecordingPublisher(
        events,
        error=RuntimeError("publisher token=secret"),
    )

    report = make_job(
        events,
        plan,
        publisher=publisher,
        publication_lock_release_error=RuntimeError("release token=secret"),
    ).run(plan.options)

    assert report.status == "failed"
    assert report.error == "publisher token=***"


@pytest.mark.parametrize(
    ("release_target", "expected_error"),
    (
        ("publication", "publication release token=***"),
        ("run", "run release token=***"),
    ),
)
def test_release_failure_without_primary_error_returns_sanitized_failed_report(
    plan: UniverseBackfillPlan,
    release_target: str,
    expected_error: str,
) -> None:
    events: list[object] = []
    release_errors = {
        "publication_lock_release_error": (
            RuntimeError("publication release token=secret")
            if release_target == "publication"
            else None
        ),
        "lock_release_error": (
            RuntimeError("run release token=secret")
            if release_target == "run"
            else None
        ),
    }

    report = make_job(events, plan, **release_errors).run(plan.options)

    assert report.status == "failed"
    assert report.error == expected_error


def test_global_partial_counts_are_checkpointed_and_resume_without_loss_or_duplication(
    plan: UniverseBackfillPlan,
) -> None:
    first_events: list[object] = []
    first_history = RecordingHistory(first_events, decision("start"))
    partial = BackfillReport(
        planned=2,
        completed=1,
        failed=1,
        results=(DetailedResult(7, 6, 1, 5, 1, 2),),
        failures=(
            PlanFailure(
                dataset=Dataset.SECURITY,
                request_key="security:global",
                error_code="provider_error",
                message="page failed",
                retryable=True,
            ),
        ),
    )

    first_report = make_job(
        first_events,
        plan,
        history=first_history,
        batch_reports={"global": partial},
    ).run(plan.options)

    assert first_report.status == "partial"
    assert first_history.checkpoints == [(0, False, first_report.counts)]

    second_events: list[object] = []
    persisted_counts = first_history.checkpoints[-1][2]
    second_history = RecordingHistory(
        second_events,
        decision("resume", counts=persisted_counts),
    )
    remaining = BackfillReport(
        planned=1,
        completed=1,
        failed=0,
        results=(DetailedResult(4, 4, 0, 4, 0, 1),),
    )

    resumed_report = make_job(
        second_events,
        plan,
        history=second_history,
        batch_reports={"global": remaining},
    ).run(plan.options)

    assert resumed_report.counts.to_payload() == {
        "providerCount": 11,
        "retainedCount": 10,
        "filteredCount": 1,
        "rowCount": 9,
        "rejectedCount": 1,
        "requestCount": 3,
    }


def test_stock_partial_counts_are_checkpointed_and_resume_without_loss_or_duplication(
    plan: UniverseBackfillPlan,
) -> None:
    initial_counts = UniverseBackfillCounts(
        provider_count=10,
        retained_count=9,
        filtered_count=1,
        row_count=8,
        request_count=2,
    )
    first_events: list[object] = []
    first_history = RecordingHistory(
        first_events,
        decision(
            "resume",
            global_completed=True,
            next_batch_index=2,
            counts=initial_counts,
        ),
    )
    partial = BackfillReport(
        planned=2,
        completed=1,
        failed=1,
        results=(DetailedResult(7, 6, 1, 5, 1, 2),),
        failures=(
            PlanFailure(
                dataset=Dataset.DAILY,
                request_key="daily:batch-2",
                error_code="provider_error",
                message="page failed",
                retryable=True,
            ),
        ),
    )

    first_report = make_job(
        first_events,
        plan,
        history=first_history,
        batch_reports={2: partial},
    ).run(plan.options)

    assert first_report.status == "partial"
    assert first_history.checkpoints == [(2, True, first_report.counts)]

    second_events: list[object] = []
    persisted_counts = first_history.checkpoints[-1][2]
    second_history = RecordingHistory(
        second_events,
        decision(
            "resume",
            global_completed=True,
            next_batch_index=2,
            counts=persisted_counts,
        ),
    )
    remaining = BackfillReport(
        planned=1,
        completed=1,
        failed=0,
        results=(DetailedResult(4, 4, 0, 4, 0, 1),),
    )

    resumed_report = make_job(
        second_events,
        plan,
        history=second_history,
        batch_reports={2: remaining},
    ).run(plan.options)

    assert resumed_report.counts.to_payload() == {
        "providerCount": 21,
        "retainedCount": 19,
        "filteredCount": 2,
        "rowCount": 17,
        "rejectedCount": 1,
        "requestCount": 5,
    }


def test_failed_batch_checkpoint_error_does_not_replace_primary_failure(
    plan: UniverseBackfillPlan,
) -> None:
    events: list[object] = []
    failed = BackfillReport(
        planned=1,
        completed=0,
        failed=1,
        results=(DetailedResult(3, 2, 1, 2, 0, 1),),
        failures=(
            PlanFailure(
                dataset=Dataset.DAILY,
                request_key="daily:batch-2",
                error_code="provider_error",
                message="provider token=secret",
                retryable=True,
            ),
        ),
    )

    report = make_job(
        events,
        plan,
        begin=decision("resume", global_completed=True, next_batch_index=2),
        batch_reports={2: failed},
        checkpoint_error=RuntimeError("checkpoint token=secret"),
    ).run(plan.options)

    assert report.status == "partial"
    assert report.failures[0].message == "provider token=***"
    assert ("history.finish", "partial") in events
