from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import app.stocks.jobs.daily_sync as daily_sync_module
from app.connectors.tushare.strategies import Dataset, RequestPlan, TushareMode
from app.stocks.jobs.daily_sync import (
    DailySyncExecutionContext,
    DailySyncJob,
    DailySyncOptions,
    DailySyncReport,
)
from app.stocks.jobs.daily_sync_plans import DailySyncPlanBuilder
from app.stocks.jobs.daily_sync_repository import DisclosureTarget
from app.marts.contracts import QualityResult
from app.stocks.universes.models import UniverseSelection


TEST_UNIVERSE = UniverseSelection(
    profile="representative-50",
    version="2026-09-04-v1",
    coverage="representative",
    symbols=("000001.SZ", "300750.SZ", "600519.SH"),
    fingerprint="representative-fingerprint",
)
FULL_UNIVERSE = UniverseSelection(
    profile="all",
    version="snapshot-full",
    coverage="full-market",
    symbols=("000001.SZ", "300750.SZ", "600519.SH"),
    fingerprint="full-market-fingerprint",
)


class FakeLock:
    def __init__(
        self,
        acquired: bool = True,
        *,
        release_error: Exception | None = None,
    ) -> None:
        self.acquired = acquired
        self.release_error = release_error
        self.acquired_keys: list[str] = []
        self.released_keys: list[str] = []

    def try_acquire(self, value: str) -> bool:
        self.acquired_keys.append(value)
        return self.acquired

    def release(self, value: str) -> None:
        self.released_keys.append(value)
        if self.release_error is not None:
            raise self.release_error


class FakeHistory:
    def __init__(self, *, completed: bool = False) -> None:
        self.completed = completed
        self.started: list[
            tuple[
                date,
                TushareMode,
                UniverseSelection,
                DailySyncExecutionContext,
            ]
        ] = []
        self.completed_queries: list[tuple[date, UniverseSelection]] = []
        self.terminal: list[dict[str, object]] = []
        self.failures = []
        self.finishes = []

    def has_completed(
        self,
        business_date: date,
        universe: UniverseSelection,
    ) -> bool:
        self.completed_queries.append((business_date, universe))
        return self.completed

    def start(
        self,
        business_date: date,
        mode: TushareMode,
        universe: UniverseSelection,
        *,
        execution: DailySyncExecutionContext,
    ) -> str:
        self.started.append((business_date, mode, universe, execution))
        return "daily-run-1"

    def record_terminal(
        self,
        business_date: date,
        mode: TushareMode,
        universe: UniverseSelection,
        *,
        status: str,
        details: dict[str, object],
        execution: DailySyncExecutionContext,
    ) -> str:
        self.terminal.append(
            {
                "business_date": business_date,
                "mode": mode,
                "universe": universe,
                "status": status,
                "details": details,
                "execution": execution,
            }
        )
        return f"terminal-{status}"

    def record_failure(self, run_id: str, failure) -> None:
        self.failures.append((run_id, failure))

    def finish(self, run_id: str, finish) -> None:
        self.finishes.append((run_id, finish))


class FakeRepository:
    def __init__(
        self,
        *,
        trading_day: bool | None = True,
        calendar_after: bool | None = True,
        has_securities: bool = True,
        disclosures: tuple[DisclosureTarget, ...] = (),
        corporate: tuple[str, ...] = (),
    ) -> None:
        self.current_trading_day = trading_day
        self.calendar_after = calendar_after
        self.active_securities = has_securities
        self.disclosures = disclosures
        self.corporate = corporate
        self.calendar_queries = 0
        self.disclosure_queries: list[tuple[date, date, tuple[str, ...]]] = []
        self.corporate_queries: list[tuple[date, date, tuple[str, ...]]] = []

    def published_workbenches(self, allowed_symbols, business_date):
        return ()

    def trading_day(self, business_date: date) -> bool | None:
        self.calendar_queries += 1
        return self.current_trading_day

    def has_active_securities(self) -> bool:
        return self.active_securities

    def disclosure_targets(
        self,
        start_date: date,
        end_date: date,
        allowed_symbols: tuple[str, ...],
    ) -> tuple[DisclosureTarget, ...]:
        self.disclosure_queries.append((start_date, end_date, allowed_symbols))
        return tuple(
            target for target in self.disclosures if target.symbol in allowed_symbols
        )

    def corporate_symbols(
        self,
        start_date: date,
        end_date: date,
        allowed_symbols: tuple[str, ...],
    ) -> tuple[str, ...]:
        self.corporate_queries.append((start_date, end_date, allowed_symbols))
        return tuple(symbol for symbol in self.corporate if symbol in allowed_symbols)


class FakeRunner:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.calls: list[RequestPlan] = []
        self.behaviors: dict[Dataset, str] = {}

    def __call__(self, plan: RequestPlan):
        self.calls.append(plan)
        if plan.dataset is Dataset.TRADE_CALENDAR:
            self.repository.current_trading_day = self.repository.calendar_after
        if plan.dataset is Dataset.SECURITY:
            self.repository.active_securities = True

        behavior = self.behaviors.get(plan.dataset)
        result = SimpleNamespace(
            dataset=plan.dataset,
            request_key=plan.cursor_key,
            request_count=2,
            received_count=5,
            retained_count=4,
            filtered_count=1,
            row_count=3,
            completed=behavior != "incomplete",
            rejected_count=1 if behavior == "rejected" else 0,
        )
        if behavior == "raise":
            error = RuntimeError(
                "provider failed token=secret-token "
                "postgresql+psycopg://user:db-password@localhost/db"
            )
            error.partial_result = result
            error.error_code = "provider_error"
            error.retryable = True
            error.request_plan = plan
            error.mode = TushareMode.STANDARD
            raise error
        return result


class FakeBuildJob:
    def __init__(self, *, status: str = "completed", error: str | None = None) -> None:
        self.calls = []
        self.report = SimpleNamespace(
            status=status,
            job_run_id="build-run-1",
            version="20260902-a1b2c3d4" if status == "completed" else None,
            release_version=(
                "20260902-a1b2c3d4" if status == "completed" else None
            ),
            products=("market-overview", "stock-workbench:600519.SH"),
            promoted_products=(
                "market-overview",
                "stock-workbench:600519.SH",
            ) if status == "completed" else (),
            current_versions=(
                ("market-overview", "20260902-a1b2c3d4"),
                ("stock-workbench:600519.SH", "20260902-a1b2c3d4"),
            ) if status == "completed" else (),
            quality=(QualityResult("all", "blocking", status == "completed", {}),),
            error=error,
        )

    def run(self, options):
        self.calls.append(options)
        return self.report


def make_job(
    *,
    repository: FakeRepository | None = None,
    history: FakeHistory | None = None,
    lock: FakeLock | None = None,
    publication_lock: FakeLock | None = None,
    build: FakeBuildJob | None = None,
    universe: UniverseSelection = TEST_UNIVERSE,
) -> tuple[DailySyncJob, FakeRunner, FakeRepository, FakeHistory, FakeLock, FakeBuildJob]:
    repository = repository or FakeRepository()
    history = history or FakeHistory()
    lock = lock or FakeLock()
    build = build or FakeBuildJob()
    runner = FakeRunner(repository)
    job = DailySyncJob(
        runner=runner,
        plan_builder_factory=DailySyncPlanBuilder,
        repository=repository,
        build_job=build,
        history=history,
        lock=lock,
        publication_lock=publication_lock or FakeLock(),
        universe=universe,
        sanitize_error=lambda message: message
        .replace("secret-token", "***")
        .replace("db-password", "***"),
    )
    return job, runner, repository, history, lock, build


def standard_options(**changes: object) -> DailySyncOptions:
    values: dict[str, object] = {
        "business_date": date(2026, 9, 2),
        "mode": TushareMode.STANDARD,
    }
    values.update(changes)
    return DailySyncOptions(**values)


def test_report_payload_is_stable_and_exact() -> None:
    report = DailySyncReport(
        status="completed",
        job_run_id="daily-run-1",
        business_date=date(2026, 9, 2),
        mode=TushareMode.STANDARD,
        universe=TEST_UNIVERSE,
        request_count=17,
        provider_count=50,
        retained_count=42,
        filtered_count=8,
        row_count=42,
        rejected_count=0,
        affected_symbols=("600519.SH",),
        version="20260902-a1b2c3d4",
        products=("market-overview", "stock-workbench:600519.SH"),
    )

    assert report.to_payload() == {
        "status": "completed",
        "jobRunId": "daily-run-1",
        "businessDate": "2026-09-02",
        "mode": "standard",
        "universe": TEST_UNIVERSE.to_payload(),
        "requestCount": 17,
        "providerCount": 50,
        "retainedCount": 42,
        "filteredCount": 8,
        "rowCount": 42,
        "rejectedCount": 0,
        "affectedSymbols": ["600519.SH"],
        "failedDatasets": [],
        "version": "20260902-a1b2c3d4",
        "products": ["market-overview", "stock-workbench:600519.SH"],
        "quality": [],
    }


def test_lock_conflict_persists_terminal_without_work() -> None:
    job, runner, _repository, history, lock, build = make_job(lock=FakeLock(False))

    report = job.run(standard_options())

    assert report.status == "locked"
    assert report.job_run_id == "terminal-locked"
    assert runner.calls == []
    assert build.calls == []
    assert history.terminal[0]["status"] == "locked"
    assert history.terminal[0]["universe"] is TEST_UNIVERSE
    assert history.terminal[0]["execution"].to_details() == {"trigger": "cli"}
    assert lock.released_keys == []


def test_daily_sync_uses_shared_publication_lock_key_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daily_sync_module,
        "daily_sync_lock_key",
        lambda business_date: f"daily:{business_date.isoformat()}",
    )
    monkeypatch.setattr(
        daily_sync_module,
        "data_publication_lock_key",
        lambda: "publication:global",
    )
    publication_lock = FakeLock()
    job, _runner, _repository, _history, lock, _build = make_job(
        publication_lock=publication_lock,
    )

    report = job.run(standard_options())

    assert report.status == "completed"
    assert lock.acquired_keys == ["daily:2026-09-02"]
    assert lock.released_keys == ["daily:2026-09-02"]
    assert publication_lock.acquired_keys == ["publication:global"]
    assert publication_lock.released_keys == ["publication:global"]


def test_daily_sync_holds_global_publication_lock_only_while_building() -> None:
    publication_lock = FakeLock()
    job, runner, _repository, _history, _lock, build = make_job(
        publication_lock=publication_lock,
    )
    original_runner = runner.__call__
    original_build = build.run

    def run_without_publication_lock(plan: RequestPlan):
        assert publication_lock.acquired_keys == []
        return original_runner(plan)

    def build_with_publication_lock(options: object):
        assert publication_lock.acquired_keys == ["data-publication"]
        assert publication_lock.released_keys == []
        return original_build(options)

    job._runner = run_without_publication_lock
    build.run = build_with_publication_lock  # type: ignore[method-assign]

    report = job.run(standard_options())

    assert report.status == "completed"
    assert publication_lock.released_keys == ["data-publication"]


def test_publication_unlock_failure_preserves_completed_work_audit() -> None:
    publication_lock = FakeLock(
        release_error=RuntimeError("publication release token=secret-token"),
    )
    job, runner, _repository, history, lock, build = make_job(
        publication_lock=publication_lock,
    )

    report = job.run(standard_options())

    assert report.status == "failed"
    assert report.error == "publication release token=***"
    assert report.publication_release_error == "publication release token=***"
    assert report.request_count == len(runner.calls) * 2
    assert report.provider_count == len(runner.calls) * 5
    assert report.row_count == len(runner.calls) * 3
    assert report.version == build.report.version
    assert report.release_version == build.report.release_version
    assert report.promoted_products == tuple(sorted(build.report.promoted_products))
    assert report.current_versions == build.report.current_versions
    assert report.products == tuple(sorted(build.report.products))
    assert report.quality == build.report.quality
    assert report.failed_datasets == ()
    finish = history.finishes[-1][1]
    assert finish.request_count == report.request_count
    assert finish.row_count == report.row_count
    assert finish.details["buildJobRunId"] == "build-run-1"
    assert finish.details["releaseVersion"] == build.report.release_version
    assert finish.details["promotedProducts"] == list(
        build.report.promoted_products
    )
    assert finish.details["currentVersions"] == dict(build.report.current_versions)
    assert finish.details["publicationReleaseError"] == (
        "publication release token=***"
    )
    assert publication_lock.released_keys == ["data-publication"]
    assert lock.released_keys == ["sync-daily:2026-09-02"]


def test_publication_unlock_failure_preserves_nonblocking_partial_audit() -> None:
    publication_lock = FakeLock(
        release_error=RuntimeError("publication release token=secret-token"),
    )
    job, runner, _repository, history, _lock, build = make_job(
        publication_lock=publication_lock,
    )
    runner.behaviors[Dataset.REPURCHASE] = "raise"

    report = job.run(standard_options())

    assert report.status == "failed"
    assert report.error == "publication release token=***"
    assert report.publication_release_error == "publication release token=***"
    assert report.failed_datasets == (Dataset.REPURCHASE,)
    assert report.request_count == len(runner.calls) * 2
    assert report.provider_count == len(runner.calls) * 5
    assert report.row_count == len(runner.calls) * 3
    assert report.version == build.report.version
    finish = history.finishes[-1][1]
    assert finish.details["failedDatasets"] == ["repurchase"]
    assert finish.details["providerCount"] == report.provider_count
    assert finish.details["publicationReleaseError"] == (
        "publication release token=***"
    )


def test_daily_sync_preserves_build_error_when_publication_unlock_also_fails() -> None:
    publication_lock = FakeLock(
        release_error=RuntimeError("publication release token=secret-token"),
    )
    build = FakeBuildJob()

    def raise_build(_options: object) -> object:
        raise RuntimeError("build token=secret-token")

    build.run = raise_build  # type: ignore[method-assign]
    job, _runner, _repository, history, lock, _build = make_job(
        build=build,
        publication_lock=publication_lock,
    )

    report = job.run(standard_options())

    assert report.status == "failed"
    assert report.error == "build token=***"
    assert publication_lock.released_keys == ["data-publication"]
    assert lock.released_keys == ["sync-daily:2026-09-02"]
    assert history.finishes[-1][1].error_message == "build token=***"


def test_daily_sync_preserves_build_error_when_both_unlocks_fail() -> None:
    daily_lock = FakeLock(
        release_error=RuntimeError("daily release token=secret-token"),
    )
    publication_lock = FakeLock(
        release_error=RuntimeError("publication release token=secret-token"),
    )
    build = FakeBuildJob()

    def raise_build(_options: object) -> object:
        raise RuntimeError("build token=secret-token")

    build.run = raise_build  # type: ignore[method-assign]
    job, _runner, _repository, _history, _lock, _build = make_job(
        build=build,
        lock=daily_lock,
        publication_lock=publication_lock,
    )

    report = job.run(standard_options())

    assert report.status == "failed"
    assert report.error == "build token=***"
    assert publication_lock.released_keys == ["data-publication"]
    assert daily_lock.released_keys == ["sync-daily:2026-09-02"]


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_daily_sync_control_signal_survives_both_unlock_failures(
    signal: type[BaseException],
) -> None:
    daily_lock = FakeLock(
        release_error=RuntimeError("daily release token=secret-token"),
    )
    publication_lock = FakeLock(
        release_error=RuntimeError("publication release token=secret-token"),
    )
    build = FakeBuildJob()

    def interrupt_build(_options: object) -> object:
        raise signal()

    build.run = interrupt_build  # type: ignore[method-assign]
    job, _runner, _repository, _history, _lock, _build = make_job(
        build=build,
        lock=daily_lock,
        publication_lock=publication_lock,
    )

    with pytest.raises(signal):
        job.run(standard_options())

    assert publication_lock.released_keys == ["data-publication"]
    assert daily_lock.released_keys == ["sync-daily:2026-09-02"]


def test_completed_run_skips_under_lock_unless_forced() -> None:
    history = FakeHistory(completed=True)
    job, runner, _repository, history, lock, build = make_job(history=history)

    skipped = job.run(standard_options())

    assert skipped.status == "skipped"
    assert runner.calls == []
    assert build.calls == []
    assert history.terminal[0]["status"] == "skipped"
    assert history.terminal[0]["execution"].to_details() == {"trigger": "cli"}
    assert history.completed_queries == [(date(2026, 9, 2), TEST_UNIVERSE)]
    assert lock.released_keys == ["sync-daily:2026-09-02"]

    forced = job.run(standard_options(force=True))
    assert forced.status == "completed"
    assert build.calls


def test_unknown_calendar_is_fetched_first_and_requeried() -> None:
    repository = FakeRepository(trading_day=None, calendar_after=True)
    job, runner, repository, _history, _lock, build = make_job(repository=repository)

    report = job.run(standard_options())

    assert report.status == "completed"
    assert runner.calls[0].dataset is Dataset.TRADE_CALENDAR
    assert repository.calendar_queries == 2
    assert build.calls


def test_closed_day_finishes_skipped_without_ingestion_or_build() -> None:
    repository = FakeRepository(trading_day=False)
    job, runner, _repository, history, lock, build = make_job(repository=repository)

    report = job.run(standard_options())

    assert report.status == "skipped"
    assert runner.calls == []
    assert build.calls == []
    assert history.finishes[-1][1].status == "skipped"
    assert lock.released_keys == ["sync-daily:2026-09-02"]


def test_security_bootstrap_precedes_nonblocking_classification_and_market() -> None:
    repository = FakeRepository(has_securities=False)
    job, runner, _repository, history, _lock, build = make_job(repository=repository)
    runner.behaviors[Dataset.CLASSIFICATION] = "raise"

    report = job.run(standard_options())

    datasets = [plan.dataset for plan in runner.calls]
    assert datasets.index(Dataset.SECURITY) < datasets.index(Dataset.CLASSIFICATION)
    assert datasets.index(Dataset.CLASSIFICATION) < datasets.index(Dataset.DAILY)
    assert report.status == "partial"
    assert report.failed_datasets == (Dataset.CLASSIFICATION,)
    assert history.failures[0][1].dataset is Dataset.CLASSIFICATION
    assert build.calls


@pytest.mark.parametrize("behavior", ("raise", "incomplete", "rejected"))
def test_critical_market_failure_blocks_build_and_releases_lock(behavior: str) -> None:
    job, runner, _repository, history, lock, build = make_job()
    runner.behaviors[Dataset.DAILY] = behavior

    report = job.run(standard_options())

    assert report.status == "failed"
    assert build.calls == []
    assert history.failures[0][1].dataset is Dataset.DAILY
    expected_code = {
        "raise": "provider_error",
        "incomplete": "incomplete_page",
        "rejected": "rejected_rows",
    }[behavior]
    assert history.failures[0][1].error_code == expected_code
    assert lock.released_keys == ["sync-daily:2026-09-02"]


def test_discovery_window_merges_explicit_disclosure_and_corporate_symbols() -> None:
    repository = FakeRepository(
        disclosures=(DisclosureTarget("600519.SH", date(2026, 6, 30)),),
        corporate=("000001.SZ", "600519.SH"),
    )
    job, runner, repository, _history, _lock, build = make_job(repository=repository)

    report = job.run(
        standard_options(symbols=("300750.SZ", "000001.SZ"), lookback_days=3)
    )

    assert repository.disclosure_queries == [
        (date(2026, 8, 31), date(2026, 9, 2), TEST_UNIVERSE.symbols)
    ]
    assert repository.corporate_queries == [
        (date(2026, 8, 31), date(2026, 9, 2), TEST_UNIVERSE.symbols)
    ]
    disclosure_dates = [
        plan.params["actual_date"]
        for plan in runner.calls
        if plan.dataset is Dataset.DISCLOSURE_DATE
    ]
    assert disclosure_dates == ["20260831", "20260901", "20260902"]
    income_symbols = [
        plan.params["ts_code"]
        for plan in runner.calls
        if plan.dataset is Dataset.INCOME
    ]
    assert income_symbols == ["000001.SZ", "300750.SZ", "600519.SH"]
    assert report.affected_symbols == ("000001.SZ", "300750.SZ", "600519.SH")
    assert build.calls[0].symbols == ()
    assert build.calls[0].workbench_symbols == report.affected_symbols


def test_vip_financial_statement_plans_use_disclosed_periods() -> None:
    repository = FakeRepository(
        disclosures=(
            DisclosureTarget("600519.SH", date(2026, 6, 30)),
            DisclosureTarget("000001.SZ", date(2026, 3, 31)),
        )
    )
    job, runner, _repository, _history, _lock, _build = make_job(repository=repository)

    report = job.run(standard_options(mode=TushareMode.VIP))

    assert report.status == "completed"
    assert [
        plan.params for plan in runner.calls if plan.dataset is Dataset.INCOME
    ] == [{"period": "20260331"}, {"period": "20260630"}]
    assert [
        plan.params for plan in runner.calls if plan.dataset is Dataset.DIVIDEND
    ] == [{"ts_code": "000001.SZ"}, {"ts_code": "600519.SH"}]


def test_nonblocking_failure_publishes_partial_and_counts_partial_result() -> None:
    job, runner, _repository, history, _lock, build = make_job()
    runner.behaviors[Dataset.REPURCHASE] = "raise"

    report = job.run(standard_options())

    assert report.status == "partial"
    assert Dataset.REPURCHASE in report.failed_datasets
    assert report.request_count == len(runner.calls) * 2
    assert report.row_count == len(runner.calls) * 3
    assert report.provider_count == len(runner.calls) * 5
    assert report.retained_count == len(runner.calls) * 4
    assert report.filtered_count == len(runner.calls)
    assert history.failures
    assert build.calls
    finish = history.finishes[-1][1]
    assert finish.details["buildJobRunId"] == "build-run-1"
    assert finish.details["affectedSymbols"] == []
    assert finish.details["failedDatasets"] == ["repurchase"]
    assert finish.details["universe"] == TEST_UNIVERSE.to_payload()
    assert finish.details["providerCount"] == report.provider_count
    assert [stage["name"] for stage in finish.details["stages"]] == [
        "classification",
        "critical-market",
        "discovery",
        "financial",
    ]


def test_build_failure_returns_failed_with_quality_and_sanitized_error() -> None:
    build = FakeBuildJob(
        status="failed",
        error="publish failed token=secret-token db_password=db-password",
    )
    job, _runner, _repository, history, lock, _build = make_job(build=build)

    report = job.run(standard_options(symbols=("600519.SH",)))
    payload = report.to_payload()

    assert report.status == "failed"
    assert report.quality == build.report.quality
    assert set(payload["error"]) == {"errorType", "errorCode", "message"}
    assert "secret-token" not in payload["error"]["message"]
    assert "db-password" not in payload["error"]["message"]
    assert history.finishes[-1][1].status == "failed"
    assert lock.released_keys == ["sync-daily:2026-09-02"]


def test_unexpected_repository_error_returns_failed_and_releases_lock() -> None:
    repository = FakeRepository()

    def fail_disclosure(
        _start_date: date,
        _end_date: date,
        _allowed_symbols: tuple[str, ...],
    ):
        raise RuntimeError("query failed token=secret-token")

    repository.disclosure_targets = fail_disclosure
    job, _runner, _repository, history, lock, build = make_job(
        repository=repository
    )

    report = job.run(standard_options())

    assert report.status == "failed"
    assert report.job_run_id == "daily-run-1"
    assert report.error == "query failed token=***"
    assert build.calls == []
    assert history.finishes[-1][1].status == "failed"
    assert lock.released_keys == ["sync-daily:2026-09-02"]


def test_explicit_symbol_outside_universe_fails_before_ingestion() -> None:
    job, runner, _repository, history, lock, build = make_job()

    report = job.run(standard_options(symbols=("999999.SH",)))

    assert report.status == "failed"
    assert report.error == (
        "symbol 999999.SH is outside universe representative-50"
    )
    assert runner.calls == []
    assert build.calls == []
    assert history.started == [
        (
            date(2026, 9, 2),
            TushareMode.STANDARD,
            TEST_UNIVERSE,
            DailySyncExecutionContext(),
        )
    ]
    assert lock.released_keys == ["sync-daily:2026-09-02"]


def test_universe_profiles_share_the_same_business_date_lock_key() -> None:
    representative_lock = FakeLock(False)
    full_lock = FakeLock(False)
    representative_job = make_job(lock=representative_lock)[0]
    full_job = make_job(lock=full_lock, universe=FULL_UNIVERSE)[0]

    representative_job.run(standard_options())
    full_job.run(standard_options())

    assert representative_lock.acquired_keys == ["sync-daily:2026-09-02"]
    assert full_lock.acquired_keys == ["sync-daily:2026-09-02"]
