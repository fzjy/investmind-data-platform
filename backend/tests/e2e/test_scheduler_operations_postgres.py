from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
import httpx
import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.orm import Session, sessionmaker

import app.stocks.jobs.daily_sync_history as history_module
import app.stocks.jobs.daily_sync_runtime as runtime_module
from app.config import PROJECT_ROOT, Settings
from app.connectors.tushare.client import TushareClient
from app.db.models import (
    JobFailure,
    JobRun,
    MacroSeriesState,
    OperationalAlert,
    PublishedSnapshot,
    SchedulerCheckpoint,
    SchedulerHeartbeat,
    TradeCalendar,
)
from app.stocks.jobs.daily_sync_history import daily_sync_business_key
from app.stocks.jobs.daily_sync_runtime import DailySyncRuntime, open_daily_sync_runtime
from app.jobs.freshness import DAILY_PRODUCTS, FreshnessService
from app.jobs.locks import PostgresAdvisoryJobLock
from app.stocks.jobs.scheduler_coordinator import ScheduledDailySyncCoordinator
from app.stocks.jobs.scheduler_planning import CatchupPlanner, ExpectedBusinessDateResolver
from app.jobs.scheduler_state import SchedulerStateRepository
from app.jobs.sync_status import SyncStatusService
from app.stocks.universes.models import UniverseSelection
from backend.tests.e2e.test_daily_sync_postgres import (
    EXCLUDED_SYMBOL,
    SYMBOL,
    TEST_TOKEN,
    DailySyncTushare,
    PostgresContainer,
    _assert_persisted_universe,
    _configure_environment,
)


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL scheduler tests",
    ),
]

FIRST_START = datetime.fromisoformat("2026-09-02T19:30:00+08:00")
SECOND_START = datetime.fromisoformat("2026-09-15T20:00:00+08:00")
HEARTBEAT_TIMEOUT = timedelta(minutes=5)
EARLIER_OPEN_DATES = (
    date(2026, 8, 27),
    date(2026, 8, 28),
    date(2026, 8, 31),
    date(2026, 9, 1),
)
ALL_OPEN_DATES = tuple(date(2026, 9, day) for day in range(2, 16))


class SchedulerTushare(DailySyncTushare):
    def __init__(self) -> None:
        super().__init__()
        self.fail_requests = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.fail_requests:
            return super().handle(request)
        payload: dict[str, Any] = json.loads(request.content)
        params = payload["params"]
        assert isinstance(params, dict)
        self.requests.append((str(payload["api_name"]), dict(params)))
        return httpx.Response(503, request=request, text="controlled provider failure")


class InjectedDateTime(datetime):
    current = FIRST_START.astimezone(UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


def test_scheduler_operations_survive_restart_and_recover_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        source = SchedulerTushare()
        universe_file = tmp_path / "custom-universe.json"
        universe_file.write_text(json.dumps([SYMBOL]), encoding="utf-8")
        _configure_environment(monkeypatch, tmp_path, database_url)
        monkeypatch.setenv("DANG_SYNC_UNIVERSE", "custom")
        monkeypatch.setenv("DANG_SYNC_UNIVERSE_FILE", str(universe_file))
        monkeypatch.setattr(history_module, "datetime", InjectedDateTime)

        def controlled_client(token: str, **_kwargs: object) -> TushareClient:
            assert token == TEST_TOKEN
            return TushareClient(
                token,
                transport=httpx.MockTransport(source.handle),
                max_retries=0,
            )

        monkeypatch.setattr(runtime_module, "TushareClient", controlled_client)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT current_setting('server_version_num')::int / 10000")
                ) == 17
                assert connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                ) == ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "backend/alembic.ini"))).get_current_head()
            _seed_open_dates(
                sessionmaker(engine),
                (*EARLIER_OPEN_DATES, ALL_OPEN_DATES[0]),
            )

            settings = Settings.from_env()
            with open_daily_sync_runtime(settings, "custom", universe_file) as runtime:
                state = SchedulerStateRepository(runtime.session_factory)
                planner = CatchupPlanner(state, runtime.selection, max_days=10)
                resolver = ExpectedBusinessDateResolver(
                    state,
                    "Asia/Shanghai",
                    19,
                    0,
                )
                freshness = FreshnessService(
                    runtime.session_factory,
                    timezone="Asia/Shanghai",
                )
                first_instance = state.start_instance(
                    "daily-sync",
                    now=FIRST_START,
                    details=runtime.selection.to_payload(),
                )
                first = _coordinator(
                    state,
                    planner,
                    resolver,
                    freshness,
                    runtime,
                    first_instance,
                )

                before_first = freshness.inspect(
                    expected_business_date=ALL_OPEN_DATES[0],
                    missing_business_dates=(ALL_OPEN_DATES[0],),
                    checked_at=FIRST_START,
                )
                assert before_first.missing_products == tuple(sorted(DAILY_PRODUCTS))
                assert before_first.missing_business_dates == (ALL_OPEN_DATES[0],)

                assert state.open_dates(
                    start=EARLIER_OPEN_DATES[0],
                    end=ALL_OPEN_DATES[0],
                ) == (*EARLIER_OPEN_DATES, ALL_OPEN_DATES[0])
                assert state.checkpoint(runtime.selection) is None
                with runtime.session_factory() as session:
                    assert session.scalar(select(func.count(JobRun.id))) == 0
                assert source.requests == []

                InjectedDateTime.current = FIRST_START.astimezone(UTC)
                first_report = first.run_startup(FIRST_START)
                assert first_report.attempted_dates == (date(2026, 9, 2),)
                assert first_report.remaining_dates == ()
                assert [report.status for report in first_report.reports] == ["completed"]
                checkpoint = state.checkpoint(runtime.selection)
                assert checkpoint is not None
                assert checkpoint.managed_from == date(2026, 9, 2)
                assert state.completed_dates(
                    runtime.selection,
                    (*EARLIER_OPEN_DATES, date(2026, 9, 2)),
                ) == frozenset({date(2026, 9, 2)})
                with runtime.session_factory() as session:
                    assert session.scalars(
                        select(JobRun.business_key).where(JobRun.job_name == "sync-daily")
                    ).all() == [
                        daily_sync_business_key(date(2026, 9, 2), runtime.selection)
                    ]
                assert {
                    params["trade_date"]
                    for api_name, params in source.requests
                    if api_name in {"daily", "daily_basic", "adj_factor"}
                } == {"20260902"}
                earlier_request_dates = {
                    business_date.strftime("%Y%m%d")
                    for business_date in EARLIER_OPEN_DATES
                }
                for _api_name, params in source.requests:
                    for date_key in ("trade_date", "start_date", "end_date"):
                        assert params.get(date_key) not in earlier_request_dates

                _seed_open_dates(runtime.session_factory, ALL_OPEN_DATES)
                offline_instance = state.start_instance(
                    "daily-sync",
                    now=datetime.fromisoformat("2026-09-03T19:00:00+08:00"),
                    details=runtime.selection.to_payload(),
                )
                online_instance = state.start_instance(
                    "daily-sync",
                    now=SECOND_START,
                    details=runtime.selection.to_payload(),
                )
                interrupted_id = _add_running_run(
                    runtime.session_factory,
                    runtime.selection,
                    date(2026, 9, 3),
                    datetime.fromisoformat("2026-09-03T19:05:00+08:00"),
                    {"trigger": "scheduler", "schedulerInstanceId": offline_instance},
                )
                manual_id = _add_running_run(
                    runtime.session_factory,
                    runtime.selection,
                    date(2026, 9, 4),
                    datetime.fromisoformat("2026-09-04T19:05:00+08:00"),
                    {"trigger": "cli"},
                )
                no_instance_id = _add_running_run(
                    runtime.session_factory,
                    runtime.selection,
                    date(2026, 9, 5),
                    datetime.fromisoformat("2026-09-05T19:05:00+08:00"),
                    {"trigger": "scheduler"},
                )
                online_id = _add_running_run(
                    runtime.session_factory,
                    runtime.selection,
                    date(2026, 9, 6),
                    datetime.fromisoformat("2026-09-06T19:05:00+08:00"),
                    {"trigger": "scheduler", "schedulerInstanceId": online_instance},
                )
                second = _coordinator(
                    state,
                    planner,
                    resolver,
                    freshness,
                    runtime,
                    online_instance,
                )

                recovered = second.recover_interrupted(SECOND_START)
                assert recovered == (interrupted_id,)
                with runtime.session_factory() as session:
                    statuses = dict(
                        session.execute(
                            select(JobRun.id, JobRun.status).where(
                                JobRun.id.in_(
                                    (interrupted_id, manual_id, no_instance_id, online_id)
                                )
                            )
                        ).all()
                    )
                    failure = session.scalar(
                        select(JobFailure).where(
                            JobFailure.job_run_id == interrupted_id
                        )
                    )
                assert statuses == {
                    interrupted_id: "failed",
                    manual_id: "running",
                    no_instance_id: "running",
                    online_id: "running",
                }
                assert failure is not None
                assert failure.error_code == "SCHEDULER_INTERRUPTED"

                contender = PostgresAdvisoryJobLock(engine)
                request_count = len(source.requests)
                assert contender.try_acquire("sync-daily:2026-09-03") is True
                try:
                    locked = second.run_one(date(2026, 9, 3), SECOND_START)
                finally:
                    contender.release("sync-daily:2026-09-03")
                assert locked.status == "locked"
                assert len(source.requests) == request_count

                InjectedDateTime.current = SECOND_START.astimezone(UTC)
                catchup = second.run_startup(SECOND_START)
                assert catchup.attempted_dates == tuple(
                    date(2026, 9, day) for day in range(3, 13)
                )
                assert len(catchup.attempted_dates) == 10
                assert catchup.remaining_dates == tuple(
                    date(2026, 9, day) for day in range(13, 16)
                )
                assert all(report.status == "completed" for report in catchup.reports)

                checkpoint_after_catchup = state.checkpoint(runtime.selection)
                assert checkpoint_after_catchup is not None
                assert checkpoint_after_catchup.managed_from == date(2026, 9, 2)
                assert checkpoint_after_catchup.details["backlog_count"] == 13
                assert checkpoint_after_catchup.details["max_days"] == 10
                assert checkpoint_after_catchup.details["limit_exceeded"] is True
                backlog_alert = state.alert(_backlog_alert_key(runtime.selection))
                assert backlog_alert is not None
                assert backlog_alert.status == "open"
                assert backlog_alert.details["remainingCount"] == 3
                assert backlog_alert.details["limitExceeded"] is True

                request_count = len(source.requests)
                completed = second.run_one(date(2026, 9, 2), SECOND_START)
                assert completed.status == "skipped"
                assert len(source.requests) == request_count

                status = SyncStatusService(
                    state=state,
                    resolver=resolver,
                    planner=planner,
                    freshness=freshness,
                    universe=runtime.selection,
                    timezone="Asia/Shanghai",
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    catchup_max_days=10,
                )
                latest = state.latest_instance("daily-sync")
                assert latest is not None
                assert latest.instance_id == online_instance
                backlog_status = status.check(SECOND_START).to_payload()
                assert backlog_status["status"] == "degraded"
                scheduler_payload = backlog_status["scheduler"]
                assert isinstance(scheduler_payload, dict)
                assert scheduler_payload["online"] is True
                sync_payload = backlog_status["sync"]
                assert isinstance(sync_payload, dict)
                assert sync_payload["catchup"] == {
                    "assessed": True,
                    "maxDays": 10,
                    "backlogCount": 3,
                    "missingBusinessDates": [
                        "2026-09-13",
                        "2026-09-14",
                        "2026-09-15",
                    ],
                    "catchupLimitExceeded": False,
                }
                freshness_payload = backlog_status["freshness"]
                assert isinstance(freshness_payload, dict)
                assert freshness_payload["staleProducts"]

                timeout_at = SECOND_START - HEARTBEAT_TIMEOUT - timedelta(seconds=1)
                state.touch_instance(
                    online_instance,
                    now=timeout_at,
                    next_run_at=None,
                    state="running",
                )
                assert status.check(SECOND_START).scheduler["online"] is False
                state.touch_instance(
                    online_instance,
                    now=SECOND_START,
                    next_run_at=None,
                    state="stopping",
                )
                assert status.check(SECOND_START).scheduler["online"] is False
                state.touch_instance(
                    online_instance,
                    now=SECOND_START,
                    next_run_at=None,
                    state="running",
                )
                assert status.check(SECOND_START).scheduler["online"] is True

                final_now = datetime.fromisoformat("2026-09-15T20:05:00+08:00")
                InjectedDateTime.current = final_now.astimezone(UTC)
                final_catchup = second.run_startup(final_now)
                assert final_catchup.attempted_dates == tuple(
                    date(2026, 9, day) for day in range(13, 16)
                )
                assert final_catchup.remaining_dates == ()
                assert all(
                    report.status == "completed" for report in final_catchup.reports
                )
                healthy_freshness = freshness.inspect(
                    expected_business_date=date(2026, 9, 15),
                    missing_business_dates=(),
                    checked_at=final_now,
                )
                assert healthy_freshness.missing_business_dates == ()
                assert healthy_freshness.missing_products == ()
                assert healthy_freshness.stale_products == ()

                _seed_open_dates(runtime.session_factory, (date(2026, 9, 16),))
                source.fail_requests = True
                failure_now = datetime.fromisoformat("2026-09-15T21:00:00+08:00")
                InjectedDateTime.current = failure_now.astimezone(UTC)
                failed_reports = tuple(
                    second.run_one(date(2026, 9, 16), failure_now)
                    for _ in range(3)
                )
                assert [report.status for report in failed_reports] == [
                    "failed",
                    "failed",
                    "failed",
                ]
                failure_key = _failure_alert_key(runtime.selection)
                failure_alert = state.alert(failure_key)
                assert failure_alert is not None
                assert failure_alert.status == "open"
                assert failure_alert.details["consecutiveFailures"] == 3
                with runtime.session_factory() as session:
                    assert session.scalar(
                        select(func.count(OperationalAlert.id)).where(
                            OperationalAlert.alert_key == failure_key
                        )
                    ) == 1

                reevaluated_at = datetime.fromisoformat("2026-09-15T21:05:00+08:00")
                assert second.evaluate_failures(reevaluated_at) == 3
                updated_alert = state.alert(failure_key)
                assert updated_alert is not None
                assert updated_alert.id == failure_alert.id
                assert updated_alert.first_triggered_at == failure_alert.first_triggered_at
                assert updated_alert.last_triggered_at == reevaluated_at
                assert updated_alert.details["consecutiveFailures"] == 3

                source.fail_requests = False
                recovered_at = datetime.fromisoformat("2026-09-15T22:00:00+08:00")
                InjectedDateTime.current = recovered_at.astimezone(UTC)
                recovered_report = second.run_one(date(2026, 9, 16), recovered_at)
                assert recovered_report.status in {"partial", "completed"}
                resolved_alert = state.alert(failure_key)
                assert resolved_alert is not None
                assert resolved_alert.id == failure_alert.id
                assert resolved_alert.status == "resolved"
                assert resolved_alert.resolved_at == recovered_at

                for _api_name, params in source.requests:
                    assert EXCLUDED_SYMBOL not in str(params.get("ts_code", ""))
                _assert_persisted_universe(database_url)

                state.touch_instance(
                    online_instance,
                    now=recovered_at,
                    next_run_at=None,
                    state="running",
                )
                query_at = recovered_at + timedelta(minutes=1)
                assert timedelta(0) < query_at - recovered_at < HEARTBEAT_TIMEOUT
                InjectedDateTime.current = query_at.astimezone(UTC)
                before_status = _read_only_state(runtime.session_factory)
                with runtime.session_factory() as session:
                    runtime_engine = session.get_bind()
                assert isinstance(runtime_engine, Engine)
                status_statements: list[str] = []

                def capture_status_sql(
                    _connection: Connection,
                    _cursor: object,
                    statement: str,
                    _parameters: object,
                    _context: ExecutionContext,
                    _executemany: bool,
                ) -> None:
                    status_statements.append(" ".join(statement.upper().split()))

                event.listen(runtime_engine, "before_cursor_execute", capture_status_sql)
                try:
                    final_report = status.check(query_at)
                finally:
                    event.remove(
                        runtime_engine, "before_cursor_execute", capture_status_sql
                    )
                final_status = final_report.to_payload()
                after_status = _read_only_state(runtime.session_factory)
                assert before_status == after_status
                assert final_report.checked_at == query_at
                assert status_statements
                assert any(sql.startswith("SELECT ") for sql in status_statements)
                assert not [
                    sql for sql in status_statements if " FOR UPDATE" in sql
                ], status_statements
                assert not [
                    sql
                    for sql in status_statements
                    if re.search(r"\b(?:INSERT|UPDATE|DELETE)\b", sql)
                ], status_statements
                assert final_status["status"] == "ok"
                assert final_report.scheduler["online"] is True
                final_sync = final_status["sync"]
                assert isinstance(final_sync, dict)
                final_catchup_payload = final_sync["catchup"]
                assert isinstance(final_catchup_payload, dict)
                assert final_catchup_payload["missingBusinessDates"] == []
                final_freshness = final_status["freshness"]
                assert isinstance(final_freshness, dict)
                assert final_freshness["staleProducts"] == []
        finally:
            engine.dispose()


def _coordinator(
    state: SchedulerStateRepository,
    planner: CatchupPlanner,
    resolver: ExpectedBusinessDateResolver,
    freshness: FreshnessService,
    runtime: DailySyncRuntime,
    instance_id: str,
) -> ScheduledDailySyncCoordinator:
    return ScheduledDailySyncCoordinator(
        state=state,
        planner=planner,
        resolver=resolver,
        job=runtime.job,
        freshness=freshness,
        universe=runtime.selection,
        instance_id=instance_id,
        mode=runtime.mode,
        timezone="Asia/Shanghai",
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        failure_alert_threshold=3,
        lookback_days=1,
    )


def test_macro_startup_latest_slot_retry_lock_and_read_only_status(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from app.jobs.locks import data_publication_lock_key
    from app.macro.jobs.macro_scheduler import MacroScheduledCoordinator
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.macro.jobs.macro_sync_runtime import open_macro_sync_runtime
    from app.jobs.sync_status import MacroStatusReader
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog

    # All DB URLs come from this disposable container; every provider request
    # is handled in memory, and no real Tushare client is constructible.
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        _configure_environment(monkeypatch, tmp_path, url)
        engine = create_engine(url)
        try:
            factory = sessionmaker(engine, expire_on_commit=False)
            settings = Settings(database_url=url, tushare_token=None, fred_api_key="test-fred-key", macro_history_years=1)
            catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
            history = SqlMacroSyncHistory(factory)
            state = SchedulerStateRepository(factory)
            requests = []

            def reject(request):
                requests.append(request.url.path)
                return httpx.Response(401, json={"error": "controlled denial"})

            @contextmanager
            def runtime():
                with open_macro_sync_runtime(settings, transport=httpx.MockTransport(reject)) as active:
                    yield active

            now = datetime.fromisoformat("2026-09-27T12:00:00+00:00")
            def coordinator():
                return MacroScheduledCoordinator(
                    settings=settings, catalog=catalog, history=history, state=state,
                    runtime_factory=runtime, publication_lock=PostgresAdvisoryJobLock(engine), clock=lambda: now,
                )

            contender = PostgresAdvisoryJobLock(engine)
            assert contender.try_acquire(data_publication_lock_key())
            try:
                assert coordinator().run_startup(now).status == "locked"
                assert requests == []
            finally:
                contender.release(data_publication_lock_key())
            first = coordinator()
            report = first.run_startup(now)
            assert report.status == "failed"
            assert report.business_key == "2026-W39:" + catalog.catalog_version
            assert first.run_startup(now) is None
            assert requests
            second = coordinator().run_startup(now)
            assert second.attempt == report.attempt + 1
            with factory() as session:
                runs = session.scalars(select(JobRun).where(JobRun.job_name == "sync-macro")).all()
                assert len(runs) == 3
                assert {row.business_key for row in runs} == {report.business_key}
                assert all(row.details["trigger"] == "scheduler" for row in runs)
                # 缺少 Tushare token 和没有在线来源的指标均没有实际请求。
                attempted_ids = {
                    series.id for series in catalog.series
                    if any(source.provider != "tushare" for source in series.sources)
                }
                assert set(session.scalars(select(MacroSeriesState.series_id))) == attempted_ids
            summary_sql = []
            def capture(conn, cursor, sql, params, ctx, many):
                summary_sql.append(sql.strip().upper())
            before = _read_only_state(factory)
            event.listen(engine, "before_cursor_execute", capture)
            try:
                payload = MacroStatusReader(factory, settings, catalog=catalog).read(datetime.now(UTC))
            finally:
                event.remove(engine, "before_cursor_execute", capture)
            assert payload["series"]["missing"] == 20
            assert payload["openFailures"] > 0 and payload["failureSummaries"]
            assert _read_only_state(factory) == before
            assert summary_sql and all(sql.startswith("SELECT") for sql in summary_sql)
            completed = history.start(report.business_key, now, catalog.catalog_version, catalog.catalog_hash, "scheduler", 4)
            history.finish(completed, "completed", {})
            previous = len(requests)
            assert coordinator().run_startup(now) is None
            assert len(requests) == previous
        finally:
            engine.dispose()


def _seed_open_dates(
    factory: sessionmaker[Session],
    dates: tuple[date, ...],
) -> None:
    with factory.begin() as session:
        for business_date in dates:
            session.execute(
                insert(TradeCalendar)
                .values(
                    exchange="SSE",
                    cal_date=business_date,
                    is_open=True,
                    pretrade_date=business_date - timedelta(days=1),
                )
                .on_conflict_do_nothing(
                    index_elements=(TradeCalendar.exchange, TradeCalendar.cal_date),
                )
            )


@pytest.mark.parametrize("status", ["partial", "failed", "completed"])
def test_macro_cleanup_failure_preserves_persisted_health_and_advisory_order(monkeypatch, tmp_path, status):
    from contextlib import contextmanager
    from app.jobs.locks import data_publication_lock_key
    from app.macro.jobs.macro_scheduler import MacroScheduledCoordinator
    from app.macro.jobs.macro_sync import MacroSyncReport
    from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
    from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        _configure_environment(monkeypatch, tmp_path, url)
        engine = create_engine(url)
        try:
            factory = sessionmaker(engine, expire_on_commit=False)
            catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
            history = SqlMacroSyncHistory(factory)
            state = SchedulerStateRepository(factory)
            now = datetime.fromisoformat("2026-09-27T12:00:00+00:00")
            state.open_alert("macro-sync:degraded", "macro_sync", "error", now, {})
            if status != "completed":
                state.resolve_alert("macro-sync:degraded", now)
            key = "2026-W39:" + catalog.catalog_version
            class Job:
                def run(self, options, *, on_completed):
                    lock = PostgresAdvisoryJobLock(engine)
                    assert lock.try_acquire(data_publication_lock_key())
                    try:
                        run_id = history.start(key, options.as_of, catalog.catalog_version, catalog.catalog_hash, "scheduler", 1)
                        history.finish(run_id, status, {})
                        def checked(report):
                            contender = PostgresAdvisoryJobLock(engine)
                            assert contender.try_acquire(data_publication_lock_key()) is False
                            return on_completed(report)
                        return checked(MacroSyncReport(status, run_id, key, 1, options.as_of, catalog.catalog_version))
                    finally:
                        lock.release(data_publication_lock_key())
            failure = RuntimeError("controlled close failure")
            expected = "resolved" if status == "completed" else "open"
            @contextmanager
            def runtime():
                yield MacroSyncRuntime(Job(), catalog)
                assert state.alert("macro-sync:degraded").status == expected
                contender = PostgresAdvisoryJobLock(engine)
                # Job releases only after persisted health, before runtime cleanup.
                assert contender.try_acquire(data_publication_lock_key()) is True
                contender.release(data_publication_lock_key())
                raise failure
            coordinator = MacroScheduledCoordinator(
                settings=Settings(database_url=url), catalog=catalog, history=history,
                state=state, runtime_factory=runtime, publication_lock=PostgresAdvisoryJobLock(engine),
                clock=lambda: now,
            )
            with pytest.raises(RuntimeError) as caught:
                coordinator.run_startup(now)
            assert caught.value is failure
            assert state.alert("macro-sync:degraded").status == expected
            assert history.inspect(key, catalog.catalog_version, catalog.catalog_hash)[0] is (status == "completed")
            contender = PostgresAdvisoryJobLock(engine)
            assert contender.try_acquire(data_publication_lock_key())
            contender.release(data_publication_lock_key())
        finally:
            engine.dispose()


def _add_running_run(
    factory: sessionmaker[Session],
    universe: UniverseSelection,
    business_date: date,
    started_at: datetime,
    execution: dict[str, object],
) -> str:
    run_id = str(uuid4())
    with factory.begin() as session:
        session.add(
            JobRun(
                id=run_id,
                job_name="sync-daily",
                business_key=daily_sync_business_key(business_date, universe),
                status="running",
                started_at=started_at,
                details={**execution, "universe": universe.to_payload()},
            )
        )
    return run_id


def _failure_alert_key(universe: UniverseSelection) -> str:
    return (
        "sync-daily:consecutive-failures:"
        f"{universe.profile}:{universe.version}"
    )


def _backlog_alert_key(universe: UniverseSelection) -> str:
    return f"scheduler:catchup-backlog:{universe.profile}:{universe.version}"


def _read_only_state(factory: sessionmaker[Session]) -> tuple[object, ...]:
    with factory() as session:
        heartbeats = tuple(
            tuple(row)
            for row in session.execute(
                select(
                    SchedulerHeartbeat.instance_id,
                    SchedulerHeartbeat.state,
                    SchedulerHeartbeat.started_at,
                    SchedulerHeartbeat.last_heartbeat_at,
                    SchedulerHeartbeat.next_run_at,
                    SchedulerHeartbeat.details,
                ).order_by(SchedulerHeartbeat.instance_id)
            )
        )
        checkpoints = tuple(
            tuple(row)
            for row in session.execute(
                select(
                    SchedulerCheckpoint.checkpoint_key,
                    SchedulerCheckpoint.managed_from,
                    SchedulerCheckpoint.created_at,
                    SchedulerCheckpoint.last_evaluated_at,
                    SchedulerCheckpoint.details,
                ).order_by(SchedulerCheckpoint.checkpoint_key)
            )
        )
        alerts = tuple(
            tuple(row)
            for row in session.execute(
                select(
                    OperationalAlert.id,
                    OperationalAlert.alert_key,
                    OperationalAlert.status,
                    OperationalAlert.first_triggered_at,
                    OperationalAlert.last_triggered_at,
                    OperationalAlert.resolved_at,
                    OperationalAlert.details,
                ).order_by(OperationalAlert.id)
            )
        )
        snapshots = tuple(
            tuple(row)
            for row in session.execute(
                select(
                    PublishedSnapshot.product,
                    PublishedSnapshot.version,
                    PublishedSnapshot.as_of,
                    PublishedSnapshot.stale,
                    PublishedSnapshot.stale_reason,
                    PublishedSnapshot.freshness_checked_at,
                ).order_by(PublishedSnapshot.product)
            )
        )
    return heartbeats, checkpoints, alerts, snapshots
