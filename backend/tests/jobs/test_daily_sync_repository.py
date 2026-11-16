from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    DisclosureSchedule,
    Repurchase,
    Security,
    ShareholderCount,
    ShareUnlock,
    TradeCalendar,
)
from app.stocks.jobs.daily_sync_repository import DailySyncRepository, DisclosureTarget
from app.stocks.universes.models import UniverseSecurityState


@pytest.fixture
def repository_fixture(pg_factory) -> Iterator[
    tuple[DailySyncRepository, sessionmaker[Session]]
]:
    engine, factory = pg_factory.kw["bind"], pg_factory

    with factory.begin() as session:
        session.add_all(
            [
                Security(
                    ts_code="000001.SZ",
                    symbol="000001",
                    name="Ping An Bank",
                    status="L",
                ),
                Security(
                    ts_code="600519.SH",
                    symbol="600519",
                    name="Kweichow Moutai",
                    status="L",
                ),
                Security(
                    ts_code="300001.SZ",
                    symbol="300001",
                    name="Delisted Security",
                    status="D",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                TradeCalendar(
                    exchange="SSE", cal_date=date(2026, 9, 2), is_open=True
                ),
                TradeCalendar(
                    exchange="SSE", cal_date=date(2026, 9, 3), is_open=False
                ),
                TradeCalendar(
                    exchange="SZSE", cal_date=date(2026, 9, 4), is_open=True
                ),
            ]
        )
        session.add_all(
            [
                DisclosureSchedule(
                    ts_code="600519.SH",
                    end_date=date(2026, 6, 30),
                    actual_date=date(2026, 8, 27),
                ),
                DisclosureSchedule(
                    ts_code="000001.SZ",
                    end_date=date(2026, 6, 30),
                    actual_date=date(2026, 9, 2),
                ),
                DisclosureSchedule(
                    ts_code="000001.SZ",
                    end_date=date(2026, 3, 31),
                    actual_date=date(2026, 8, 26),
                ),
                DisclosureSchedule(
                    ts_code="600519.SH",
                    end_date=date(2026, 9, 30),
                    actual_date=date(2026, 9, 3),
                ),
                DisclosureSchedule(
                    ts_code="300001.SZ",
                    end_date=date(2025, 12, 31),
                    actual_date=None,
                ),
            ]
        )
        session.add_all(
            [
                Repurchase(ts_code="600519.SH", ann_date=date(2026, 8, 27)),
                Repurchase(ts_code="000001.SZ", ann_date=date(2026, 8, 26)),
                Repurchase(ts_code="300001.SZ", ann_date=None),
                ShareUnlock(ts_code="000001.SZ", ann_date=date(2026, 9, 2)),
                ShareUnlock(ts_code="600519.SH", ann_date=date(2026, 9, 3)),
                ShareholderCount(
                    ts_code="000001.SZ",
                    end_date=date(2026, 6, 30),
                    ann_date=date(2026, 8, 30),
                ),
                ShareholderCount(
                    ts_code="600519.SH",
                    end_date=date(2026, 3, 31),
                    ann_date=date(2026, 9, 1),
                ),
            ]
        )

    yield DailySyncRepository(factory), factory
    engine.dispose()


def test_trading_day_distinguishes_open_closed_and_missing_dates(
    repository_fixture: tuple[DailySyncRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = repository_fixture

    assert repository.trading_day(date(2026, 9, 2)) is True
    assert repository.trading_day(date(2026, 9, 3)) is False
    assert repository.trading_day(date(2026, 9, 4)) is None


def test_has_active_securities_ignores_delisted_rows_and_reads_fresh_state(
    repository_fixture: tuple[DailySyncRepository, sessionmaker[Session]],
) -> None:
    repository, factory = repository_fixture

    assert repository.has_active_securities() is True

    with factory.begin() as session:
        for ts_code in ("000001.SZ", "600519.SH"):
            security = session.get(Security, ts_code)
            assert security is not None
            security.status = "D"

    assert repository.has_active_securities() is False


def test_active_symbols_and_security_states_are_deterministic(
    repository_fixture: tuple[DailySyncRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = repository_fixture

    assert repository.active_symbols() == ("000001.SZ", "600519.SH")
    assert repository.security_states(("600519.SH", "300001.SZ", "missing.SH")) == {
        "300001.SZ": UniverseSecurityState(status="D", is_st=False),
        "600519.SH": UniverseSecurityState(status="L", is_st=False),
    }


def test_disclosure_targets_include_window_boundaries_and_sort_results(
    repository_fixture: tuple[DailySyncRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = repository_fixture

    assert repository.disclosure_targets(
        date(2026, 8, 27),
        date(2026, 9, 2),
        ("000001.SZ", "600519.SH"),
    ) == (
        DisclosureTarget("000001.SZ", date(2026, 6, 30)),
        DisclosureTarget("600519.SH", date(2026, 6, 30)),
    )


def test_corporate_symbols_union_deduplicates_sorts_and_filters_window(
    repository_fixture: tuple[DailySyncRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = repository_fixture

    assert repository.corporate_symbols(
        date(2026, 8, 27),
        date(2026, 9, 2),
        ("000001.SZ", "600519.SH"),
    ) == (
        "000001.SZ",
        "600519.SH",
    )


def test_daily_targets_are_filtered_in_sql_by_allowed_symbols(
    repository_fixture: tuple[DailySyncRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = repository_fixture

    assert repository.disclosure_targets(
        date(2026, 8, 27),
        date(2026, 9, 2),
        ("600519.SH",),
    ) == (DisclosureTarget("600519.SH", date(2026, 6, 30)),)
    assert repository.corporate_symbols(
        date(2026, 8, 27),
        date(2026, 9, 2),
        ("600519.SH",),
    ) == ("600519.SH",)
    assert repository.disclosure_targets(
        date(2026, 8, 27),
        date(2026, 9, 2),
        (),
    ) == ()
    assert repository.corporate_symbols(
        date(2026, 8, 27),
        date(2026, 9, 2),
        (),
    ) == ()
