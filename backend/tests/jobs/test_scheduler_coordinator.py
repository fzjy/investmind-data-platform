from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from app.connectors.tushare.strategies import TushareMode
from app.db.models import JobFailure, JobRun, TradeCalendar
from app.stocks.jobs.daily_sync import DailySyncOptions, DailySyncReport
from app.stocks.jobs.daily_sync_history import daily_sync_business_key
from app.stocks.jobs.scheduler_coordinator import ScheduledDailySyncCoordinator
from app.stocks.jobs.scheduler_planning import CatchupPlanner, TradeCalendarUnavailable
from app.jobs.scheduler_state import SchedulerStateRepository
from app.stocks.universes.models import UniverseSelection


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
EXPECTED = date(2026, 9, 15)
FAILURE_ALERT_KEY = (
    "sync-daily:consecutive-failures:representative-50:2026-09-04-v1"
)
CATCHUP_ALERT_KEY = (
    "scheduler:catchup-backlog:representative-50:2026-09-04-v1"
)
CALENDAR_ALERT_KEY = (
    "scheduler:trade-calendar-unavailable:representative-50:2026-09-04-v1"
)


class StaticResolver:
    def __init__(self, expected: date = EXPECTED, *, unavailable: bool = False) -> None:
        self.expected = expected
        self.unavailable = unavailable
        self.calls: list[datetime] = []

    def resolve(self, now: datetime) -> date:
        self.calls.append(now)
        if self.unavailable:
            raise TradeCalendarUnavailable("calendar unavailable")
        return self.expected


class RecordingFreshness:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def refresh(
        self,
        *,
        expected_business_date: date,
        missing_business_dates: Sequence[date],
        checked_at: datetime,
    ) -> object:
        self.calls.append(
            {
                "expected_business_date": expected_business_date,
                "missing_business_dates": tuple(missing_business_dates),
                "checked_at": checked_at,
            }
        )
        return object()


class RecordingJob:
    def __init__(
        self,
        repository: SchedulerStateRepository,
        universe: UniverseSelection,
        statuses: Sequence[str] = (),
    ) -> None:
        self._repository = repository
        self._universe = universe
        self._statuses = deque(statuses)
        self.options: list[DailySyncOptions] = []

    def run(self, options: DailySyncOptions) -> DailySyncReport:
        self.options.append(options)
        status = self._statuses.popleft() if self._statuses else "completed"
        run_id = f"coordinator-{len(self.options)}-{options.business_date.isoformat()}"
        with self._repository._session_factory.begin() as session:
            session.add(
                JobRun(
                    id=run_id,
                    job_name="sync-daily",
                    business_key=daily_sync_business_key(
                        options.business_date,
                        self._universe,
                    ),
                    status=status,
                    started_at=NOW + timedelta(seconds=len(self.options)),
                    finished_at=NOW + timedelta(seconds=len(self.options)),
                    error_message="controlled failure" if status == "failed" else None,
                    details={
                        **options.execution.to_details(),
                        "universe": self._universe.to_payload(),
                    },
                )
            )
            if status == "failed":
                session.flush()
                session.add(
                    JobFailure(
                        job_run_id=run_id,
                        dataset="daily",
                        request_key=options.business_date.isoformat(),
                        error_code="CONTROLLED_FAILURE",
                        message="controlled failure",
                        retryable=True,
                        request_payload={},
                        last_attempted_at=NOW,
                    )
                )
        return DailySyncReport(
            status=status,
            job_run_id=run_id,
            business_date=options.business_date,
            mode=options.mode,
            universe=self._universe,
            error="controlled failure" if status == "failed" else None,
        )


@pytest.fixture
def selection() -> UniverseSelection:
    return UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )


@pytest.fixture
def repository(pg_factory) -> Iterator[SchedulerStateRepository]:
    engine, factory = pg_factory.kw["bind"], pg_factory
    yield SchedulerStateRepository(factory)
    engine.dispose()


def add_calendar(
    repository: SchedulerStateRepository,
    business_dates: Sequence[date],
    *,
    is_open: bool = True,
) -> None:
    with repository._session_factory.begin() as session:
        session.add_all(
            TradeCalendar(exchange="SSE", cal_date=value, is_open=is_open)
            for value in business_dates
        )


def make_coordinator(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    *,
    statuses: Sequence[str] = (),
    max_days: int = 10,
    threshold: int = 3,
    expected: date = EXPECTED,
    timezone: str = "Asia/Shanghai",
) -> tuple[
    ScheduledDailySyncCoordinator,
    RecordingJob,
    RecordingFreshness,
    StaticResolver,
]:
    job = RecordingJob(repository, selection, statuses)
    freshness = RecordingFreshness()
    resolver = StaticResolver(expected)
    coordinator = ScheduledDailySyncCoordinator(
        state=repository,
        planner=CatchupPlanner(repository, selection, max_days=max_days),
        resolver=resolver,
        job=job,
        freshness=freshness,
        universe=selection,
        instance_id="scheduler-instance",
        mode=TushareMode.STANDARD,
        timezone=timezone,
        heartbeat_timeout=timedelta(minutes=3),
        failure_alert_threshold=threshold,
        lookback_days=7,
    )
    return coordinator, job, freshness, resolver


def test_startup_runs_only_the_oldest_batch_and_opens_backlog_from_reinspection(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    trading_dates = tuple(date(2026, 9, day) for day in range(1, 13))
    add_calendar(repository, trading_dates)
    repository.ensure_checkpoint(selection, fallback=trading_dates[0], now=NOW)
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        max_days=10,
        expected=trading_dates[-1],
    )

    report = coordinator.run_startup(NOW)

    assert report.attempted_dates == trading_dates[:10]
    assert [options.business_date for options in job.options] == list(trading_dates[:10])
    assert report.remaining_dates == trading_dates[10:]
    assert tuple(item.status for item in report.reports) == ("completed",) * 10
    alert = repository.alert(CATCHUP_ALERT_KEY)
    assert alert is not None
    assert alert.status == "open"
    assert alert.severity == "warning"
    assert alert.details["remainingCount"] == 2
    assert alert.details["selectedCount"] == 10
    assert alert.details["earliestMissingDate"] == "2026-09-11"
    assert alert.details["latestMissingDate"] == "2026-09-12"
    assert alert.details["maxDays"] == 10
    assert alert.details["limitExceeded"] is True


def test_startup_continues_after_failed_report_and_resolves_cleared_backlog(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    trading_dates = tuple(date(2026, 9, day) for day in range(1, 4))
    add_calendar(repository, trading_dates)
    repository.ensure_checkpoint(selection, fallback=trading_dates[0], now=NOW)
    repository.open_alert(
        CATCHUP_ALERT_KEY,
        "catchup-backlog",
        "warning",
        NOW - timedelta(days=1),
        {"remainingCount": 3},
    )
    coordinator, job, freshness, _resolver = make_coordinator(
        repository,
        selection,
        statuses=("failed", "completed", "partial"),
        max_days=3,
        expected=trading_dates[-1],
    )

    report = coordinator.run_startup(NOW)

    assert [options.business_date for options in job.options] == list(trading_dates)
    assert tuple(item.status for item in report.reports) == (
        "failed",
        "completed",
        "partial",
    )
    assert report.remaining_dates == (trading_dates[0], trading_dates[2])
    assert len(freshness.calls) == 2
    assert repository.alert(CATCHUP_ALERT_KEY).status == "open"

    job._statuses.extend(("completed", "completed"))
    second = coordinator.run_startup(NOW + timedelta(minutes=1))
    assert second.attempted_dates == (trading_dates[0], trading_dates[2])
    assert second.remaining_dates == ()
    assert repository.alert(CATCHUP_ALERT_KEY).status == "resolved"


def test_startup_continues_after_job_exception_and_updates_backlog(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_dates = (date(2026, 9, 1), date(2026, 9, 2))
    add_calendar(repository, trading_dates)
    repository.ensure_checkpoint(selection, fallback=trading_dates[0], now=NOW)
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        max_days=2,
        expected=trading_dates[-1],
    )
    original_run = job.run
    should_raise = True

    def raise_once(options: DailySyncOptions) -> DailySyncReport:
        nonlocal should_raise
        if should_raise:
            should_raise = False
            job.options.append(options)
            raise RuntimeError("job unavailable")
        return original_run(options)

    monkeypatch.setattr(job, "run", raise_once)

    with pytest.raises(RuntimeError, match="job unavailable"):
        coordinator.run_startup(NOW)

    assert [options.business_date for options in job.options] == list(trading_dates)
    alert = repository.alert(CATCHUP_ALERT_KEY)
    assert alert is not None
    assert alert.status == "open"
    assert alert.details["remainingCount"] == 1


def test_startup_continues_after_freshness_exception_and_resolves_backlog(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_dates = (date(2026, 9, 1), date(2026, 9, 2))
    add_calendar(repository, trading_dates)
    repository.ensure_checkpoint(selection, fallback=trading_dates[0], now=NOW)
    repository.open_alert(
        CATCHUP_ALERT_KEY,
        "catchup-backlog",
        "warning",
        NOW - timedelta(days=1),
        {"remainingCount": 2},
    )
    coordinator, job, freshness, _resolver = make_coordinator(
        repository,
        selection,
        max_days=2,
        expected=trading_dates[-1],
    )
    original_refresh = freshness.refresh
    should_raise = True

    def raise_once(**kwargs: object) -> object:
        nonlocal should_raise
        result = original_refresh(**kwargs)
        if should_raise:
            should_raise = False
            raise RuntimeError("freshness unavailable")
        return result

    monkeypatch.setattr(freshness, "refresh", raise_once)

    with pytest.raises(RuntimeError, match="freshness unavailable"):
        coordinator.run_startup(NOW)

    assert [options.business_date for options in job.options] == list(trading_dates)
    assert len(freshness.calls) == 2
    assert repository.alert(CATCHUP_ALERT_KEY).status == "resolved"


def test_startup_preserves_freshness_failure_when_failure_evaluation_also_fails(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_dates = (date(2026, 9, 1), date(2026, 9, 2))
    add_calendar(repository, trading_dates)
    repository.ensure_checkpoint(selection, fallback=trading_dates[0], now=NOW)
    repository.open_alert(
        CATCHUP_ALERT_KEY,
        "catchup-backlog",
        "warning",
        NOW - timedelta(days=1),
        {"remainingCount": 2},
    )
    coordinator, job, freshness, _resolver = make_coordinator(
        repository,
        selection,
        max_days=2,
        expected=trading_dates[-1],
    )
    original_refresh = freshness.refresh
    primary_error = RuntimeError("freshness unavailable")
    evaluation_error = RuntimeError("failure evaluation unavailable")
    should_raise_refresh = True
    should_raise_evaluation = True

    def raise_freshness_once(**kwargs: object) -> object:
        nonlocal should_raise_refresh
        result = original_refresh(**kwargs)
        if should_raise_refresh:
            should_raise_refresh = False
            raise primary_error
        return result

    def raise_evaluation_once(now: datetime) -> int:
        nonlocal should_raise_evaluation
        if should_raise_evaluation:
            should_raise_evaluation = False
            raise evaluation_error
        return 0

    monkeypatch.setattr(freshness, "refresh", raise_freshness_once)
    monkeypatch.setattr(coordinator, "evaluate_failures", raise_evaluation_once)

    with pytest.raises(RuntimeError) as raised:
        coordinator.run_startup(NOW)

    assert raised.value is primary_error
    assert str(raised.value) == "freshness unavailable"
    assert [options.business_date for options in job.options] == list(trading_dates)
    assert len(freshness.calls) == 2
    assert repository.alert(CATCHUP_ALERT_KEY).status == "resolved"


def test_startup_continues_after_failure_evaluation_exception_and_resolves_backlog(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_dates = (date(2026, 9, 1), date(2026, 9, 2))
    add_calendar(repository, trading_dates)
    repository.ensure_checkpoint(selection, fallback=trading_dates[0], now=NOW)
    repository.open_alert(
        CATCHUP_ALERT_KEY,
        "catchup-backlog",
        "warning",
        NOW - timedelta(days=1),
        {"remainingCount": 2},
    )
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        max_days=2,
        expected=trading_dates[-1],
    )
    original_evaluate = coordinator.evaluate_failures
    should_raise = True

    def raise_once(now: datetime) -> int:
        nonlocal should_raise
        if should_raise:
            should_raise = False
            raise RuntimeError("state unavailable")
        return original_evaluate(now)

    monkeypatch.setattr(coordinator, "evaluate_failures", raise_once)

    with pytest.raises(RuntimeError, match="state unavailable"):
        coordinator.run_startup(NOW)

    assert [options.business_date for options in job.options] == list(trading_dates)
    assert repository.alert(CATCHUP_ALERT_KEY).status == "resolved"


@pytest.mark.parametrize("recovery_status", ("partial", "completed"))
def test_third_failure_opens_alert_and_successful_terminal_resolves_it(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    recovery_status: str,
) -> None:
    with repository._session_factory.begin() as session:
        session.add(TradeCalendar(exchange="SSE", cal_date=EXPECTED, is_open=True))
    coordinator, _job, freshness, _resolver = make_coordinator(
        repository,
        selection,
        statuses=("failed", "failed", "failed", recovery_status),
    )

    for day in range(1, 4):
        coordinator.run_one(date(2026, 9, day), NOW + timedelta(minutes=day))

    alert = repository.alert(FAILURE_ALERT_KEY)
    assert alert is not None
    assert alert.status == "open"
    assert alert.severity == "critical"
    assert alert.details["threshold"] == 3
    assert alert.details["consecutiveFailures"] == 3
    assert alert.details["latestBusinessDate"] == "2026-09-03"
    assert alert.details["errorCode"] == "daily_sync_failed"

    coordinator.run_one(date(2026, 9, 4), NOW + timedelta(minutes=4))
    assert repository.alert(FAILURE_ALERT_KEY).status == "resolved"
    assert len(freshness.calls) == 1


def test_locked_and_skipped_reports_do_not_change_failure_streak(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    coordinator, _job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        statuses=("failed", "locked", "failed", "skipped", "failed"),
    )

    for day in range(1, 5):
        coordinator.run_one(date(2026, 9, day), NOW + timedelta(minutes=day))
        assert repository.alert(FAILURE_ALERT_KEY) is None

    coordinator.run_one(date(2026, 9, 5), NOW + timedelta(minutes=5))
    assert repository.consecutive_failures(selection) == 3
    assert repository.alert(FAILURE_ALERT_KEY).status == "open"


@pytest.mark.parametrize("ignored_status", ("locked", "skipped"))
def test_ignored_terminal_keeps_failure_alert_on_latest_counted_run(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    ignored_status: str,
) -> None:
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        statuses=("failed", "failed", "failed"),
    )
    for day in range(1, 4):
        coordinator.run_one(date(2026, 9, day), NOW + timedelta(minutes=day))
    opened = repository.alert(FAILURE_ALERT_KEY)
    assert opened is not None

    job._statuses.append(ignored_status)
    coordinator.run_one(date(2026, 9, 4), NOW + timedelta(hours=1))

    updated = repository.alert(FAILURE_ALERT_KEY)
    assert updated is not None
    assert updated.last_triggered_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)
    assert updated.details["consecutiveFailures"] == 3
    assert updated.details["latestBusinessDate"] == "2026-09-03"


def test_scheduled_run_uses_configured_local_date_and_scheduler_context(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    local_business_date = date(2026, 9, 6)
    add_calendar(repository, (local_business_date,))
    coordinator, job, freshness, _resolver = make_coordinator(
        repository,
        selection,
        expected=local_business_date,
    )
    now = datetime(2026, 9, 5, 16, 30, tzinfo=UTC)

    report = coordinator.run_scheduled(now)

    assert report is not None
    assert report.business_date == local_business_date
    assert len(job.options) == 1
    options = job.options[0]
    assert options.business_date == local_business_date
    assert options.mode is TushareMode.STANDARD
    assert options.lookback_days == 7
    assert options.execution.trigger == "scheduler"
    assert options.execution.scheduler_instance_id == "scheduler-instance"
    assert freshness.calls == [
        {
            "expected_business_date": local_business_date,
            "missing_business_dates": (),
            "checked_at": now,
        }
    ]


def test_scheduled_closed_day_skips_without_creating_job_run(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_calendar(repository, (EXPECTED,), is_open=False)
    repository.open_alert(
        CALENDAR_ALERT_KEY,
        "trade-calendar-unavailable",
        "warning",
        NOW - timedelta(days=1),
        {"businessDate": EXPECTED.isoformat()},
    )
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        timezone="UTC",
    )

    assert coordinator.run_scheduled(NOW) is None
    assert job.options == []
    assert repository.latest_daily_run(selection) is None
    assert repository.alert(CALENDAR_ALERT_KEY).status == "resolved"


def test_scheduled_unknown_calendar_opens_alert_then_resolves_when_available(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        timezone="UTC",
    )

    assert coordinator.run_scheduled(NOW) is None
    alert = repository.alert(CALENDAR_ALERT_KEY)
    assert alert is not None
    assert alert.status == "open"
    assert alert.details == {
        "businessDate": EXPECTED.isoformat(),
        "errorCode": "TRADE_CALENDAR_UNAVAILABLE",
        "timezone": "UTC",
        "universeProfile": selection.profile,
        "universeVersion": selection.version,
    }
    assert job.options == []

    add_calendar(repository, (EXPECTED,))
    assert coordinator.run_scheduled(NOW + timedelta(minutes=1)) is not None
    assert repository.alert(CALENDAR_ALERT_KEY).status == "resolved"


def test_scheduled_runs_confirmed_trading_day_before_calendar_alert_resolution(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_calendar(repository, (EXPECTED,))
    repository.open_alert(
        CALENDAR_ALERT_KEY,
        "trade-calendar-unavailable",
        "warning",
        NOW - timedelta(days=1),
        {"businessDate": EXPECTED.isoformat()},
    )
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        timezone="UTC",
    )
    original_resolve = repository.resolve_alert

    def fail_calendar_resolution(key: str, now: datetime) -> None:
        if key == CALENDAR_ALERT_KEY:
            raise RuntimeError("alert storage unavailable")
        original_resolve(key, now)

    monkeypatch.setattr(repository, "resolve_alert", fail_calendar_resolution)

    with pytest.raises(RuntimeError, match="alert storage unavailable"):
        coordinator.run_scheduled(NOW)

    assert [options.business_date for options in job.options] == [EXPECTED]


def test_startup_runs_selected_dates_before_calendar_alert_resolution(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_calendar(repository, (EXPECTED,))
    repository.ensure_checkpoint(selection, fallback=EXPECTED, now=NOW)
    repository.open_alert(
        CALENDAR_ALERT_KEY,
        "trade-calendar-unavailable",
        "warning",
        NOW - timedelta(days=1),
        {"businessDate": EXPECTED.isoformat()},
    )
    coordinator, job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        max_days=1,
        timezone="UTC",
    )
    original_resolve = repository.resolve_alert

    def fail_calendar_resolution(key: str, now: datetime) -> None:
        if key == CALENDAR_ALERT_KEY:
            raise RuntimeError("alert storage unavailable")
        original_resolve(key, now)

    monkeypatch.setattr(repository, "resolve_alert", fail_calendar_resolution)

    with pytest.raises(RuntimeError, match="alert storage unavailable"):
        coordinator.run_startup(NOW)

    assert [options.business_date for options in job.options] == [EXPECTED]


def test_recover_interrupted_delegates_timeout_and_updates_failure_alert(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    offline = repository.start_instance(
        "daily-sync",
        now=NOW - timedelta(minutes=10),
        details={},
    )
    with repository._session_factory.begin() as session:
        session.add(
            JobRun(
                id="interrupted-run",
                job_name="sync-daily",
                business_key=daily_sync_business_key(EXPECTED, selection),
                status="running",
                started_at=NOW - timedelta(minutes=9),
                details={
                    "trigger": "scheduler",
                    "schedulerInstanceId": offline,
                },
            )
        )
    coordinator, _job, _freshness, _resolver = make_coordinator(
        repository,
        selection,
        threshold=1,
    )

    assert coordinator.recover_interrupted(NOW) == ("interrupted-run",)
    alert = repository.alert(FAILURE_ALERT_KEY)
    assert alert is not None
    assert alert.status == "open"
    assert alert.details["errorCode"] == "SCHEDULER_INTERRUPTED"


def test_recover_interrupted_rejects_naive_now(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    coordinator, _job, _freshness, _resolver = make_coordinator(repository, selection)

    with pytest.raises(ValueError, match="now must be timezone-aware"):
        coordinator.recover_interrupted(NOW.replace(tzinfo=None))
