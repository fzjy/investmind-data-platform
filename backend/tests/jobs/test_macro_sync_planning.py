from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.macro.jobs.macro_series_state import MacroSeriesStateView
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, MacroSyncPlanner
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


CATALOG = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
AS_OF = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


def options(as_of: datetime = AS_OF) -> MacroSyncOptions:
    return MacroSyncOptions(
        as_of=as_of, history_years=10,
        catalog_version=CATALOG.catalog_version,
        catalog_hash=CATALOG.catalog_hash,
        force=False, trigger="cli",
    )


def state(series_id: str, observed: date) -> MacroSeriesStateView:
    return MacroSeriesStateView(
        series_id=series_id, catalog_version=CATALOG.catalog_version,
        last_checked_at=AS_OF - timedelta(days=7),
        last_success_at=AS_OF - timedelta(days=7),
        latest_observed_at=observed, latest_released_at=AS_OF - timedelta(days=7),
        active_source="tushare", fallback_used=False, stale=False,
        stale_reason=None, consecutive_failures=0,
    )


def test_first_run_uses_ten_year_window_and_catalog_order() -> None:
    plans = MacroSyncPlanner(CATALOG).plan(options(), states={})
    assert len(plans) == 20
    assert tuple(item.series.id for item in plans) == tuple(item.id for item in CATALOG.series)
    assert {item.window.start for item in plans} == {date(2016, 9, 20)}
    assert {item.window.end for item in plans} == {date(2026, 9, 20)}


def test_incremental_windows_use_calendar_overlap() -> None:
    states = {series.id: state(series.id, date(2026, 9, 1)) for series in CATALOG.series}
    plans = MacroSyncPlanner(CATALOG).plan(options(), states)
    starts = {item.series.id: item.window.start for item in plans}
    assert starts["cn.rate.dr007"] == date(2026, 8, 21)
    assert starts["cn.cpi.yoy"] == date(2025, 3, 1)
    assert starts["cn.gdp.real_yoy"] == date(2023, 10, 1)


def test_long_shutdown_expands_only_to_last_observation_with_history_cap() -> None:
    states = {"cn.cpi.yoy": state("cn.cpi.yoy", date(2020, 2, 1))}
    plans = MacroSyncPlanner(CATALOG).plan(options(), states)
    starts = {item.series.id: item.window.start for item in plans}
    assert starts["cn.cpi.yoy"] == date(2020, 2, 1)
    states["cn.cpi.yoy"] = state("cn.cpi.yoy", date(2010, 1, 1))
    plans = MacroSyncPlanner(CATALOG).plan(options(), states)
    assert next(item.window.start for item in plans if item.series.id == "cn.cpi.yoy") == date(2016, 9, 20)


def test_as_of_is_timezone_aware_and_window_uses_its_local_date() -> None:
    local_as_of = datetime(2026, 9, 21, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    plans = MacroSyncPlanner(CATALOG).plan(options(local_as_of), states={})
    assert plans[0].window.end == date(2026, 9, 21)
    assert plans[0].window.as_of == local_as_of
    with pytest.raises(ValueError, match="timezone"):
        options(datetime(2026, 9, 20))


def test_configured_timezone_controls_window_across_midnight_and_iso_week() -> None:
    utc_sunday = datetime(2026, 12, 27, 16, 30, tzinfo=UTC)
    plans = MacroSyncPlanner(CATALOG, timezone="Asia/Shanghai").plan(
        options(utc_sunday), states={},
    )
    assert plans[0].window.end == date(2026, 12, 28)
    assert plans[0].window.start == date(2016, 12, 28)
    assert plans[0].window.as_of == utc_sunday


def test_future_state_cannot_shorten_historical_replay_backfill() -> None:
    replay = datetime(2019, 9, 20, 9, tzinfo=UTC)
    future = state("cn.rate.dr007", date(2026, 9, 18))
    plans = MacroSyncPlanner(CATALOG, timezone="Asia/Shanghai").plan(
        options(replay), states={future.series_id: future},
    )
    target = next(item for item in plans if item.series.id == "cn.rate.dr007")
    assert target.window.start == date(2009, 9, 20)
    assert target.window.end == date(2019, 9, 20)
