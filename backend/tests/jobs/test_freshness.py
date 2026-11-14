from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, DerivedBase, MartBase
from app.db.models import PublishedSnapshot
from app.jobs.freshness import FreshnessService


NOW = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        execution_options={
            "schema_translate_map": {
                "raw": None,
                "core": None,
                "ops": None,
                "derived": None,
                "mart": None,
            }
        },
    )
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def published_snapshot_selects(
    factory: sessionmaker[Session],
) -> Iterator[list[str]]:
    statements: list[str] = []

    def record_select(execute_state: ORMExecuteState) -> None:
        if not execute_state.is_select:
            return
        statement = str(
            execute_state.statement.compile(dialect=postgresql.dialect())
        )
        if "published_snapshot" in statement:
            statements.append(statement)

    event.listen(factory.class_, "do_orm_execute", record_select)
    try:
        yield statements
    finally:
        event.remove(factory.class_, "do_orm_execute", record_select)


def add_pointer(
    factory: sessionmaker[Session],
    product: str,
    *,
    as_of: date | datetime,
    stale: bool = False,
    stale_reason: str | None = None,
    freshness_checked_at: datetime | None = None,
) -> None:
    persisted_as_of = (
        as_of
        if isinstance(as_of, datetime)
        else datetime.combine(as_of, time.min, tzinfo=timezone.utc)
    )
    with factory.begin() as session:
        session.add(
            PublishedSnapshot(
                product=product,
                version="v1",
                as_of=persisted_as_of,
                payload={"product": product},
                stale=stale,
                stale_reason=stale_reason,
                freshness_checked_at=freshness_checked_at,
                published_at=NOW,
            )
        )


def pointer(
    factory: sessionmaker[Session], product: str
) -> PublishedSnapshot:
    with factory() as session:
        row = session.get(PublishedSnapshot, product)
        assert row is not None
        session.expunge(row)
        return row


def test_refresh_marks_only_daily_products_stale(factory) -> None:
    add_pointer(factory, "market-overview", as_of=date(2026, 9, 3))
    add_pointer(factory, "macro-insights", as_of=date(2026, 8, 1))

    report = FreshnessService(factory).refresh(
        expected_business_date=date(2026, 9, 4),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert report.stale_products == ("market-overview",)
    market = pointer(factory, "market-overview")
    assert market.stale is True
    assert market.stale_reason == "SNAPSHOT_BEHIND_EXPECTED_DATE"
    assert market.freshness_checked_at is not None
    assert market.freshness_checked_at.replace(tzinfo=timezone.utc) == NOW
    macro = pointer(factory, "macro-insights")
    assert macro.stale is False
    assert macro.stale_reason is None
    assert macro.freshness_checked_at is None


def test_inspect_uses_configured_timezone_for_pointer_business_date(factory) -> None:
    add_pointer(
        factory,
        "market-overview",
        as_of=datetime(2026, 9, 5, 16, 30, tzinfo=timezone.utc),
    )

    report = FreshnessService(factory, timezone="Asia/Shanghai").inspect(
        expected_business_date=date(2026, 9, 6),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert report.stale_products == ()


def test_refresh_requests_postgresql_row_lock(
    factory: sessionmaker[Session],
    published_snapshot_selects: list[str],
) -> None:
    FreshnessService(factory).refresh(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert len(published_snapshot_selects) == 1
    assert "FOR UPDATE" in published_snapshot_selects[0]


def test_inspect_does_not_request_row_lock(
    factory: sessionmaker[Session],
    published_snapshot_selects: list[str],
) -> None:
    FreshnessService(factory).inspect(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert len(published_snapshot_selects) == 1
    assert "FOR UPDATE" not in published_snapshot_selects[0]


def test_middle_date_gap_degrades_report_but_keeps_current_pointers_fresh(factory) -> None:
    add_pointer(factory, "market-overview", as_of=date(2026, 9, 5))
    add_pointer(factory, "stock-workbench:600519.SH", as_of=date(2026, 9, 5))

    service = FreshnessService(factory)
    inspected = service.inspect(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(date(2026, 9, 4),),
        checked_at=NOW,
    )
    report = service.refresh(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(date(2026, 9, 4),),
        checked_at=NOW,
    )

    assert report.missing_business_dates == (date(2026, 9, 4),)
    assert report == inspected
    assert report.stale_products == ()
    assert report.healthy is False
    for product in ("market-overview", "stock-workbench:600519.SH"):
        assert pointer(factory, product).stale is False
        assert pointer(factory, product).stale_reason is None


def test_inspect_reports_policy_without_writing_pointers(factory) -> None:
    original_check = datetime(2026, 9, 1, tzinfo=timezone.utc)
    add_pointer(
        factory,
        "portfolio",
        as_of=date(2026, 9, 5),
        stale=True,
        stale_reason="OLD_REASON",
        freshness_checked_at=original_check,
    )
    add_pointer(factory, "macro-transmission", as_of=date(2026, 8, 1))

    report = FreshnessService(factory).inspect(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert report.stale_products == ()
    assert report.missing_products == (
        "market-explorer",
        "market-overview",
        "screener",
    )
    persisted = pointer(factory, "portfolio")
    assert persisted.stale is True
    assert persisted.stale_reason == "OLD_REASON"
    assert persisted.freshness_checked_at is not None
    assert persisted.freshness_checked_at.replace(tzinfo=timezone.utc) == original_check
    assert pointer(factory, "macro-transmission").freshness_checked_at is None


def test_missing_workbench_pointer_does_not_create_wildcard_product(factory) -> None:
    report = FreshnessService(factory).inspect(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert report.missing_products == (
        "market-explorer",
        "market-overview",
        "portfolio",
        "screener",
    )
    assert "stock-workbench:*" not in report.missing_products


def test_refresh_clears_existing_daily_stale_metadata_when_current(factory) -> None:
    add_pointer(
        factory,
        "screener",
        as_of=date(2026, 9, 5),
        stale=True,
        stale_reason="SNAPSHOT_BEHIND_EXPECTED_DATE",
        freshness_checked_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )

    report = FreshnessService(factory).refresh(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(),
        checked_at=NOW,
    )

    assert report.stale_products == ()
    refreshed = pointer(factory, "screener")
    assert refreshed.stale is False
    assert refreshed.stale_reason is None
    assert refreshed.freshness_checked_at is not None
    assert refreshed.freshness_checked_at.replace(tzinfo=timezone.utc) == NOW


def test_refresh_rolls_back_all_pointer_updates_when_flush_fails(factory) -> None:
    add_pointer(factory, "market-overview", as_of=date(2026, 9, 3))
    add_pointer(factory, "portfolio", as_of=date(2026, 9, 3))

    def fail_flush(_session, _flush_context, _instances) -> None:
        raise RuntimeError("flush failed")

    event.listen(factory.class_, "before_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="flush failed"):
            FreshnessService(factory).refresh(
                expected_business_date=date(2026, 9, 4),
                missing_business_dates=(),
                checked_at=NOW,
            )
    finally:
        event.remove(factory.class_, "before_flush", fail_flush)

    for product in ("market-overview", "portfolio"):
        persisted = pointer(factory, product)
        assert persisted.stale is False
        assert persisted.stale_reason is None
        assert persisted.freshness_checked_at is None


def test_middle_gap_alone_is_unhealthy_and_clears_old_pointer_staleness(factory) -> None:
    for product in ("market-overview", "market-explorer", "screener", "portfolio"):
        add_pointer(
            factory, product, as_of=date(2026, 9, 5), stale=True,
            stale_reason="SNAPSHOT_BEHIND_EXPECTED_DATE",
        )
    report = FreshnessService(factory).refresh(
        expected_business_date=date(2026, 9, 5),
        missing_business_dates=(date(2026, 9, 4),),
        checked_at=NOW,
    )
    assert report.stale_products == ()
    assert report.missing_products == ()
    assert report.healthy is False
    assert pointer(factory, "portfolio").stale is False
    assert pointer(factory, "portfolio").stale_reason is None
