from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from app.jobs.freshness import FreshnessReport
from app.stocks.jobs.scheduler_planning import CatchupPlan, TradeCalendarUnavailable
from app.jobs.scheduler_state import (
    AlertView,
    CheckpointView,
    DailyRunView,
    HeartbeatView,
)
from app.jobs.sync_status import SyncStatusService
from app.stocks.universes.models import UniverseSelection


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
EXPECTED = date(2026, 9, 5)


class FakeState:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.heartbeat = HeartbeatView(
            instance_id="scheduler-1",
            scheduler_name="daily-sync",
            state="running",
            started_at=datetime(2026, 9, 5, 10, 0),
            last_heartbeat_at=datetime(2026, 9, 5, 11, 59, 30),
            next_run_at=datetime(2026, 9, 6, 11, 0),
            details={},
        )
        self.checkpoint_view = CheckpointView(
            key="sync-daily:representative-50:2026-09-04-v1",
            managed_from=date(2026, 9, 1),
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
            last_evaluated_at=NOW,
            details={},
        )
        self.latest_run = DailyRunView(
            id="run-1",
            business_date=EXPECTED,
            status="completed",
            started_at=datetime(2026, 9, 5, 11, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 5, 11, 3, tzinfo=UTC),
            request_count=1,
            row_count=1,
            error_message=None,
            details={},
        )
        self.failure_count = 0
        self.alerts: tuple[AlertView, ...] = ()

    def latest_instance(self, scheduler_name: str) -> HeartbeatView | None:
        self.calls.append("latest_instance")
        assert scheduler_name == "daily-sync"
        return self.heartbeat

    def checkpoint(self, universe: UniverseSelection) -> CheckpointView | None:
        self.calls.append("checkpoint")
        return self.checkpoint_view

    def latest_daily_run(
        self,
        universe: UniverseSelection,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> DailyRunView | None:
        self.calls.append("latest_daily_run")
        assert statuses is None
        return self.latest_run

    def consecutive_failures(self, universe: UniverseSelection) -> int:
        self.calls.append("consecutive_failures")
        return self.failure_count

    def open_alerts(self) -> tuple[AlertView, ...]:
        self.calls.append("open_alerts")
        return self.alerts

    def start_instance(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not start a scheduler instance")

    def touch_instance(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not touch a scheduler instance")

    def stop_instance(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not stop a scheduler instance")

    def ensure_checkpoint(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not create a checkpoint")

    def update_checkpoint_evaluation(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not update a checkpoint")

    def open_alert(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not open an alert")

    def resolve_alert(self, *_: object, **__: object) -> None:
        raise AssertionError("status check must not resolve an alert")


class FakeResolver:
    def __init__(self) -> None:
        self.error: TradeCalendarUnavailable | None = None

    def resolve(self, now: datetime) -> date:
        assert now == NOW
        if self.error is not None:
            raise self.error
        return EXPECTED


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[date] = []
        self.result = CatchupPlan(
            expected_business_date=EXPECTED,
            managed_from=date(2026, 9, 1),
            missing_dates=(),
            selected_dates=(),
            max_days=10,
        )

    def inspect(self, expected: date) -> CatchupPlan:
        self.calls.append(expected)
        assert expected == EXPECTED
        return self.result


def test_unavailable_calendar_marks_backlog_and_freshness_unassessed(harness):
    harness.resolver.error = TradeCalendarUnavailable("incomplete")
    payload = harness.service.check(NOW).to_payload()
    assert payload["sync"]["catchup"]["assessed"] is False
    assert payload["sync"]["catchup"]["backlogCount"] is None
    assert payload["freshness"]["assessed"] is False
    assert payload["status"] == "degraded"


class FakeFreshness:
    def __init__(self) -> None:
        self.stale_products: tuple[str, ...] = ()
        self.missing_products: tuple[str, ...] = ()
        self.calls: list[tuple[date, tuple[date, ...], datetime]] = []

    def inspect(
        self,
        *,
        expected_business_date: date,
        missing_business_dates: tuple[date, ...],
        checked_at: datetime,
    ) -> FreshnessReport:
        self.calls.append(
            (expected_business_date, missing_business_dates, checked_at)
        )
        return FreshnessReport(
            checked_at=checked_at,
            expected_business_date=expected_business_date,
            stale_products=self.stale_products,
            missing_products=self.missing_products,
            missing_business_dates=missing_business_dates,
        )


@dataclass(frozen=True)
class StatusHarness:
    service: SyncStatusService
    state: FakeState
    resolver: FakeResolver
    planner: FakePlanner
    freshness: FakeFreshness


@pytest.fixture
def harness() -> StatusHarness:
    universe = UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )
    state = FakeState()
    resolver = FakeResolver()
    planner = FakePlanner()
    freshness = FakeFreshness()
    service = SyncStatusService(
        state=state,
        resolver=resolver,
        planner=planner,
        freshness=freshness,
        universe=universe,
        timezone="Asia/Shanghai",
        heartbeat_timeout=timedelta(minutes=3),
    )
    return StatusHarness(service, state, resolver, planner, freshness)


def test_status_is_ok_only_when_scheduler_and_data_are_healthy(
    harness: StatusHarness,
) -> None:
    report = harness.service.check(NOW)

    assert report.status == "ok"
    payload = report.to_payload()
    assert list(payload) == [
        "status",
        "checkedAt",
        "timezone",
        "scheduler",
        "sync",
        "freshness",
        "alerts",
        "macro",
    ]
    assert payload["checkedAt"] == "2026-09-05T12:00:00Z"
    assert payload["scheduler"]["online"] is True


def test_status_is_degraded_when_latest_snapshot_hides_middle_gap(
    harness: StatusHarness,
) -> None:
    missing = date(2026, 9, 4)
    harness.planner.result = CatchupPlan(
        expected_business_date=EXPECTED,
        managed_from=date(2026, 9, 1),
        missing_dates=(missing,),
        selected_dates=(missing,),
        max_days=10,
    )

    report = harness.service.check(NOW)

    assert report.status == "degraded"
    assert report.to_payload()["sync"]["catchup"] == {
        "assessed": True,
        "maxDays": 10,
        "backlogCount": 1,
        "missingBusinessDates": ["2026-09-04"],
        "catchupLimitExceeded": False,
    }


@pytest.mark.parametrize(
    ("heartbeat", "expected_online"),
    [
        (None, False),
        (
            HeartbeatView(
                instance_id="stale",
                scheduler_name="daily-sync",
                state="running",
                started_at=datetime(2026, 9, 5, 10, 0),
                last_heartbeat_at=datetime(2026, 9, 5, 11, 56, 59),
                next_run_at=None,
                details={},
            ),
            False,
        ),
        (
            HeartbeatView(
                instance_id="degraded",
                scheduler_name="daily-sync",
                state="degraded",
                started_at=datetime(2026, 9, 5, 10, 0),
                last_heartbeat_at=datetime(2026, 9, 5, 11, 59, 59),
                next_run_at=None,
                details={},
            ),
            True,
        ),
        (
            HeartbeatView(
                instance_id="stopping",
                scheduler_name="daily-sync",
                state="stopping",
                started_at=datetime(2026, 9, 5, 10, 0),
                last_heartbeat_at=datetime(2026, 9, 5, 11, 59, 59),
                next_run_at=None,
                details={},
            ),
            False,
        ),
    ],
)
def test_scheduler_offline_degraded_or_stopping_degrades_status(
    harness: StatusHarness,
    heartbeat: HeartbeatView | None,
    expected_online: bool,
) -> None:
    harness.state.heartbeat = heartbeat

    payload = harness.service.check(NOW).to_payload()

    assert payload["status"] == "degraded"
    assert payload["scheduler"]["online"] is expected_online


@pytest.mark.parametrize(
    ("stale_products", "missing_products"),
    [(('market-overview',), ()), ((), ('portfolio',))],
)
def test_stale_or_missing_products_degrade_status(
    harness: StatusHarness,
    stale_products: tuple[str, ...],
    missing_products: tuple[str, ...],
) -> None:
    harness.freshness.stale_products = stale_products
    harness.freshness.missing_products = missing_products

    payload = harness.service.check(NOW).to_payload()

    assert payload["status"] == "degraded"
    assert payload["freshness"]["staleProducts"] == list(stale_products)
    assert payload["freshness"]["missingProducts"] == list(missing_products)


def test_freshness_products_have_deterministic_order(
    harness: StatusHarness,
) -> None:
    harness.freshness.stale_products = ("screener", "market-overview")
    harness.freshness.missing_products = ("portfolio", "market-explorer")

    payload = harness.service.check(NOW).to_payload()

    assert payload["freshness"]["staleProducts"] == [
        "market-overview",
        "screener",
    ]
    assert payload["freshness"]["missingProducts"] == [
        "market-explorer",
        "portfolio",
    ]


def test_open_alerts_are_sorted_and_expose_only_safe_fields(
    harness: StatusHarness,
) -> None:
    def alert(identifier: int, key: str) -> AlertView:
        return AlertView(
            id=identifier,
            key=key,
            alert_type="catchup-backlog",
            severity="warning",
            status="open",
            first_triggered_at=datetime(2026, 9, 5, 10, 0),
            last_triggered_at=datetime(2026, 9, 5, 11, 0),
            resolved_at=None,
            details={"databaseUrl": "postgresql://user:secret@example/db"},
        )

    harness.state.alerts = (alert(2, "z-key"), alert(1, "a-key"))

    payload = harness.service.check(NOW).to_payload()

    assert payload["status"] == "degraded"
    assert [item["key"] for item in payload["alerts"]] == ["a-key", "z-key"]
    assert set(payload["alerts"][0]) == {
        "key",
        "alertType",
        "severity",
        "status",
        "firstTriggeredAt",
        "lastTriggeredAt",
    }
    assert "secret" not in str(payload)


def test_calendar_unavailable_returns_safe_degraded_state(
    harness: StatusHarness,
) -> None:
    harness.resolver.error = TradeCalendarUnavailable(
        "calendar failed at postgresql://user:secret@example/db"
    )

    payload = harness.service.check(NOW).to_payload()

    assert payload["status"] == "degraded"
    assert payload["sync"]["expectedBusinessDate"] is None
    assert payload["sync"]["expectedBusinessDateError"] == {
        "code": "TRADE_CALENDAR_UNAVAILABLE",
        "message": "trade calendar unavailable",
    }
    assert payload["sync"]["catchup"]["missingBusinessDates"] == []
    assert payload["freshness"] == {
        "assessed": False,
        "checkedAt": "2026-09-05T12:00:00Z",
        "staleProducts": [],
        "missingProducts": [],
    }
    assert harness.planner.calls == []
    assert harness.freshness.calls == []
    assert "secret" not in str(payload)


def test_missing_checkpoint_is_reported_without_creating_one(
    harness: StatusHarness,
) -> None:
    harness.state.checkpoint_view = None

    payload = harness.service.check(NOW).to_payload()

    assert payload["status"] == "degraded"
    assert payload["sync"]["managedFrom"] is None


def test_check_only_uses_read_operations(harness: StatusHarness) -> None:
    harness.service.check(NOW)

    assert harness.state.calls == [
        "latest_instance",
        "checkpoint",
        "latest_daily_run",
        "consecutive_failures",
        "open_alerts",
    ]
    assert harness.planner.calls == [EXPECTED]
    assert harness.freshness.calls == [(EXPECTED, (), NOW)]


def test_payload_returns_a_deep_defensive_copy(harness: StatusHarness) -> None:
    report = harness.service.check(NOW)
    first = report.to_payload()
    first["sync"]["universe"]["profile"] = "modified"
    first["sync"]["catchup"]["missingBusinessDates"].append("2099-01-01")
    first["freshness"]["staleProducts"].append("modified")

    second = report.to_payload()

    assert second["sync"]["universe"]["profile"] == "representative-50"
    assert second["sync"]["catchup"]["missingBusinessDates"] == []
    assert second["freshness"]["staleProducts"] == []


def test_check_rejects_naive_now_before_reading_state(harness: StatusHarness) -> None:
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        harness.service.check(NOW.replace(tzinfo=None))

    assert harness.state.calls == []


def test_catchup_limit_uses_total_backlog_and_sorts_dates(
    harness: StatusHarness,
) -> None:
    missing_dates = tuple(
        date(2026, 8, 20) + timedelta(days=offset)
        for offset in reversed(range(11))
    )
    harness.planner.result = CatchupPlan(
        expected_business_date=EXPECTED,
        managed_from=date(2026, 8, 20),
        missing_dates=missing_dates,
        selected_dates=missing_dates[:10],
        max_days=10,
    )

    catchup = harness.service.check(NOW).to_payload()["sync"]["catchup"]

    assert catchup["backlogCount"] == 11
    assert catchup["catchupLimitExceeded"] is True
    assert catchup["missingBusinessDates"] == sorted(
        business_date.isoformat() for business_date in missing_dates
    )


def test_consecutive_failures_degrade_status(harness: StatusHarness) -> None:
    harness.state.failure_count = 1

    payload = harness.service.check(NOW).to_payload()

    assert payload["status"] == "degraded"
    assert payload["sync"]["consecutiveFailures"] == 1


@pytest.fixture
def macro_reader(pg_factory, monkeypatch):
    import httpx
    from app.config import Settings
    from app.db.models import PublishedSnapshot
    from app.jobs import sync_status
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not construct provider/HTTP clients")

    monkeypatch.setattr(httpx, "Client", forbidden)
    from app.connectors.tushare.client import TushareClient
    from app.connectors.macro.fred import FredMacroProvider
    monkeypatch.setattr(TushareClient, "__init__", forbidden)
    monkeypatch.setattr(FredMacroProvider, "__init__", forbidden)
    engine, factory = pg_factory.kw["bind"], pg_factory
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    yield sync_status.MacroStatusReader(factory, Settings(), catalog=catalog), factory, engine, catalog
    engine.dispose()


def test_macro_status_empty_catalog_is_missing_and_read_only(macro_reader):
    from sqlalchemy import event
    reader, _, engine, _ = macro_reader
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
    result = reader.read(NOW)
    assert result["series"] == {"total": 20, "completed": 0, "fresh": 0, "fallback": 0, "stale": 0, "missing": 20}
    assert result["nextRunAt"] == "2026-09-07T01:00:00Z"
    assert result["oldestSuccessAt"] is None
    assert result["lastRun"] is None and result["snapshotVersion"] is None
    assert result["openFailures"] == 0 and result["failureSummaries"] == []
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def _status_observation(catalog, series_id="us.pce.core_yoy", **overrides):
    from decimal import Decimal
    from app.db.models import MacroObservation

    series = catalog.require(series_id)
    fields = dict(
        source="fred", series_id=series.id, name=series.name, region=series.region,
        frequency=series.frequency, unit=series.unit,
        seasonal_adjustment=series.seasonal_adjustment,
        observed_at=date(2026, 7, 1), released_at=datetime(2026, 8, 26, tzinfo=UTC),
        revision="2026-08-26", value=Decimal("3.3441430040"),
        source_url=str(series.sources[0].source_url), valid_until=None,
    )
    return MacroObservation(**{**fields, **overrides})


@pytest.mark.parametrize("first_stale_state", [True, False])
@pytest.mark.parametrize("observed_at", [date(2026, 7, 1), date(2026, 9, 1)])
def test_macro_status_core_without_success_is_stale_without_writes(
    macro_reader, first_stale_state, observed_at,
):
    from sqlalchemy import event, select
    from app.connectors.macro.contracts import ProviderSchemaError
    from app.db.models import JobRun, JobFailure, MacroSeriesState, PublishedSnapshot
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository

    reader, factory, engine, catalog = macro_reader
    now = datetime(2026, 9, 29, 14, tzinfo=UTC)
    if first_stale_state:
        MacroSeriesStateRepository(factory, catalog.catalog_version).mark_stale(
            "us.pce.core_yoy", checked_at=now,
            error=ProviderSchemaError("MACRO_VALUE_EXPIRED"),
        )
    released_at = (datetime(2026, 8, 26, tzinfo=UTC) if observed_at == date(2026, 7, 1)
                   else datetime(2026, 9, 20, tzinfo=UTC))
    with factory.begin() as session:
        session.add(_status_observation(catalog, observed_at=observed_at,
                                          released_at=released_at, revision=released_at.date().isoformat()))
        session.add(JobRun(id="pce-first-stale", job_name="sync-macro", business_key="week",
                           status="failed", started_at=now, details={}))
        session.flush()
        session.add(JobFailure(job_run_id="pce-first-stale", dataset="macro:us.pce.core_yoy",
                               request_key="fred", error_code="MACRO_VALUE_EXPIRED",
                               message="MACRO_VALUE_EXPIRED", retryable=False))
        for product in ("macro-transmission", "macro-insights"):
            session.add(PublishedSnapshot(product=product, version="old-version", as_of=now,
                                          payload={"preserved": True}, stale=False))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
    result = reader.read(now)
    assert result["series"] == {"total": 20, "completed": 0, "fresh": 0,
                                "fallback": 0, "stale": 1, "missing": 19}
    assert result["oldestSuccessAt"] is None
    assert result["snapshotVersion"] == "old-version"
    assert result["openFailures"] == 1 and result["degraded"] is True
    with factory() as session:
        state = session.get(MacroSeriesState, "us.pce.core_yoy")
        if first_stale_state:
            assert state.active_source is None and state.latest_observed_at is None
            assert state.last_success_at is None and state.consecutive_failures == 1
            assert state.stale is True and state.stale_reason == "missing"
        else:
            assert state is None
        assert session.scalar(select(JobFailure)).resolved_at is None
        snapshots = session.scalars(select(PublishedSnapshot)).all()
        assert all(row.payload == {"preserved": True} and row.stale is False for row in snapshots)
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


@pytest.mark.parametrize("invalid_record", [
    "future_release", "future_period", "expired", "newest_expired", "unregistered_source",
])
def test_macro_status_without_success_excludes_invisible_core(macro_reader, invalid_record):
    reader, factory, _, catalog = macro_reader
    now = datetime(2026, 9, 29, 14, tzinfo=UTC)
    changes = {
        "future_release": {"released_at": datetime(2026, 10, 1, tzinfo=UTC)},
        "future_period": {"observed_at": date(2026, 10, 1)},
        "expired": {"valid_until": now},
        "newest_expired": {"valid_until": now},
        "unregistered_source": {"source": "unverified-offline"},
    }
    with factory.begin() as session:
        if invalid_record == "newest_expired":
            session.add(_status_observation(catalog, revision="2026-08-01",
                                              released_at=datetime(2026, 8, 1, tzinfo=UTC)))
        session.add(_status_observation(catalog, **changes[invalid_record]))
    assert reader.read(now)["series"] == {"total": 20, "completed": 0, "fresh": 0,
                                        "fallback": 0, "stale": 0, "missing": 20}


@pytest.mark.parametrize("timezone,now,stale,missing", [
    ("Asia/Shanghai", datetime(2026, 9, 4, 18, tzinfo=UTC), 1, 19),
    ("America/New_York", datetime(2026, 9, 5, 2, tzinfo=UTC), 0, 20),
])
def test_macro_status_core_fallback_uses_local_business_date(macro_reader, timezone, now, stale, missing):
    from app.config import Settings
    from app.jobs.sync_status import MacroStatusReader

    _, factory, _, catalog = macro_reader
    with factory.begin() as session:
        session.add(_status_observation(
            catalog, "us.rate.effective_fed_funds", observed_at=date(2026, 9, 5),
            released_at=datetime(2026, 9, 4, 16, tzinfo=UTC), revision="controlled-capture",
        ))
    counts = MacroStatusReader(factory, Settings(sync_timezone=timezone), catalog=catalog).read(now)["series"]
    assert counts == {"total": 20, "completed": 0, "fresh": 0, "fallback": 0,
                      "stale": stale, "missing": missing}


@pytest.mark.parametrize("current,fresh,stale", [
    ("2026-09-29T12:00:00+00:00", 2, 0),
    ("2026-11-07T12:00:00+00:00", 2, 0),
    ("2026-11-08T12:00:00+00:00", 0, 2),
])
def test_macro_status_quarterly_expiry_matches_sync_and_rebuild(macro_reader, current, fresh, stale):
    from app.db.models import MacroSeriesState
    reader, factory, _, catalog = macro_reader
    with factory.begin() as session:
        for series_id in ("cn.gdp.real_yoy", "us.gdp.real_qoq_annualized"):
            session.add(MacroSeriesState(
                series_id=series_id, catalog_version=catalog.catalog_version,
                active_source="fred", latest_observed_at=date(2026, 4, 1),
                latest_released_at=datetime(2026, 7, 30, tzinfo=UTC), stale=False,
                last_success_at=datetime(2026, 9, 29, tzinfo=UTC), fallback_used=False))
    counts = reader.read(datetime.fromisoformat(current))["series"]
    assert counts["fresh"] == fresh
    assert counts["stale"] == stale
    assert counts["missing"] == 18


def test_macro_status_counts_expiry_missing_fallback_failure_and_snapshot(macro_reader):
    from app.db.models import JobRun, JobFailure, MacroSeriesState, PublishedSnapshot
    reader, factory, _, catalog = macro_reader
    with factory.begin() as session:
        for i, series in enumerate(catalog.series):
            if i == 19:
                continue
            session.add(MacroSeriesState(
                series_id=series.id, catalog_version=catalog.catalog_version,
                active_source="fred", latest_observed_at=date(2020, 1, 1) if i == 18 else NOW.date(),
                latest_released_at=NOW, last_checked_at=NOW,
                last_success_at=NOW - timedelta(hours=i), stale=i == 17,
                fallback_used=i in (0, 17), consecutive_failures=int(i == 17),
            ))
        session.add(JobRun(id="macro-run", job_name="sync-macro", business_key="week", status="failed", started_at=NOW, details={"password": "hidden"}))
        session.add(JobRun(id="daily-run", job_name="sync-daily", business_key="day", status="failed", started_at=NOW, details={}))
        session.flush()
        for run_id, resolved in [("macro-run", None), ("macro-run", NOW), ("daily-run", None)]:
            session.add(JobFailure(job_run_id=run_id, dataset="macro:cn.cpi.yoy", request_key="fred", error_code="TIMEOUT", message="api_key=hidden", request_payload={"token": "hidden"}, resolved_at=resolved))
        for product in ("macro-transmission", "macro-insights"):
            session.add(PublishedSnapshot(product=product, version="macro-v1", as_of=NOW, payload={}, stale=False))
    result = reader.read(NOW)
    assert result["series"] == {"total": 20, "completed": 17, "fresh": 17, "fallback": 2, "stale": 2, "missing": 1}
    assert result["oldestSuccessAt"] == "2026-09-04T18:00:00Z"
    assert result["snapshotVersion"] == "macro-v1"
    assert result["lastRun"]["jobRunId"] == "macro-run"
    assert result["openFailures"] == 1
    assert result["failureSummaries"][0]["code"] == "TIMEOUT"
    assert "hidden" not in str(result)


@pytest.mark.parametrize("status", ["completed", "partial", "failed"])
def test_macro_health_participates_in_overall_status(harness, macro_reader, status):
    from app.db.models import JobRun, MacroSeriesState, PublishedSnapshot
    reader, factory, _, catalog = macro_reader
    with factory.begin() as session:
        for series in catalog.series:
            session.add(MacroSeriesState(series_id=series.id, catalog_version=catalog.catalog_version, active_source="fred", latest_observed_at=NOW.date(), last_success_at=NOW, stale=False, fallback_used=False))
        session.add(JobRun(id="macro-run", job_name="sync-macro", business_key="week", status=status, started_at=NOW, details={}))
        for product in ("macro-transmission", "macro-insights"):
            session.add(PublishedSnapshot(product=product, version="v1", as_of=NOW, payload={}, stale=False))
    harness.service._macro_status = reader
    assert harness.service.check(NOW).status == ("ok" if status == "completed" else "degraded")
    harness.resolver.error = TradeCalendarUnavailable("unavailable")
    assert harness.service.check(NOW).to_payload()["macro"]["lastRun"]["status"] == status


def test_disabled_macro_does_not_read_database_or_degrade_daily(harness, macro_reader):
    from app.config import Settings
    from app.jobs.sync_status import MacroStatusReader
    from sqlalchemy import event
    _, factory, engine, catalog = macro_reader
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
    harness.service._macro_status = MacroStatusReader(factory, Settings(macro_sync_enabled=False), catalog=catalog)
    report = harness.service.check(NOW)
    assert report.status == "ok" and report.to_payload()["macro"]["enabled"] is False
    assert statements == []


def test_disabled_macro_ignores_its_previous_degraded_alert(harness):
    harness.state.alerts = (AlertView(
        id=1, key="macro-sync:degraded", alert_type="macro_sync", severity="error",
        status="open", first_triggered_at=NOW, last_triggered_at=NOW, resolved_at=None, details={},
    ),)
    report = harness.service.check(NOW)
    assert report.status == "ok"
    assert report.to_payload()["alerts"][0]["key"] == "macro-sync:degraded"


@pytest.mark.parametrize(("local_time", "now", "expected"), [
    ("01:30", "2026-11-01T05:45:00+00:00", "2026-11-08T06:30:00Z"),
    ("01:30", "2026-11-01T06:15:00+00:00", "2026-11-08T06:30:00Z"),
    ("02:30", "2026-03-08T07:15:00+00:00", "2026-03-08T07:30:00Z"),
    ("02:30", "2026-03-08T07:30:00+00:00", "2026-03-15T06:30:00Z"),
])
def test_macro_next_run_payload_matches_installed_trigger_dst_policy(macro_reader, local_time, now, expected):
    from app.config import Settings
    from app.jobs.scheduler import configure_scheduler, create_scheduler
    from app.jobs.sync_status import MacroStatusReader
    _, factory, _, catalog = macro_reader
    settings = Settings(sync_timezone="America/New_York", macro_sync_day_of_week="sun", macro_sync_time=local_time)
    current = datetime.fromisoformat(now)
    payload = MacroStatusReader(factory, settings, catalog=catalog).read(current)
    scheduler = configure_scheduler(create_scheduler(settings), settings, object(), macro_job=lambda: None)
    trigger = scheduler.get_job("sync-macro").trigger
    assert payload["nextRunAt"] == expected
    assert trigger.get_next_fire_time(None, current + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z") == expected
