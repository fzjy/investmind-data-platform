from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, DerivedBase, MartBase
from app.db.models import PageSnapshot, PublishedSnapshot
from app.marts.contracts import MartCandidate, MartRows, QualityResult
from app.marts.publication import (
    SnapshotCandidate,
    SnapshotPublisher,
    QualityCheck,
    QualityCheckFailed,
)


@pytest.fixture
def publisher() -> tuple[SnapshotPublisher, sessionmaker[Session]]:
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
    factory = sessionmaker(engine, expire_on_commit=False)
    return SnapshotPublisher(factory), factory


def candidate(version: str) -> SnapshotCandidate:
    return SnapshotCandidate(
        product="screener",
        version=version,
        as_of=datetime(2026, 8, 17, tzinfo=timezone.utc),
        payload={"version": version},
    )


def mart_candidate(product: str) -> MartCandidate:
    return MartCandidate(
        product=product,
        as_of=datetime(2026, 8, 17, tzinfo=timezone.utc),
        payload={"product": product},
    )


def dated_mart_candidate(product: str, day: int, version: str) -> MartCandidate:
    return MartCandidate(
        product=product,
        as_of=datetime(2026, 9, day, tzinfo=timezone.utc),
        payload={"product": product, "version": version},
    )


def passing_results() -> tuple[QualityResult, ...]:
    return (QualityResult("fixture", "blocking", True, {}),)


def test_nonmacro_earlier_in_same_utc_day_replaces_pointer(publisher):
    active, factory = publisher
    late = MartCandidate("screener", datetime(2026, 9, 14, 23, 30), {"label": "late"})
    early = MartCandidate("screener", datetime(2026, 9, 14, 0, 30), {"label": "early"})
    active.publish_many((late,), "v-late", ())
    result = active.publish_many((early,), "v-early", ())
    assert result[0].version == "v-early"
    assert result.promoted_products == ("screener",)
    with factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version))) == {"v-late", "v-early"}


def v1_candidates() -> tuple[MartCandidate, ...]:
    return (mart_candidate("screener"), mart_candidate("portfolio"))


def v2_candidates() -> tuple[MartCandidate, ...]:
    return (
        mart_candidate("screener"),
        mart_candidate("portfolio"),
    )


def test_failed_candidate_keeps_current_snapshot(publisher) -> None:
    active, _ = publisher
    active.publish(candidate("v1"), [QualityCheck("rows", lambda payload: True)])

    with pytest.raises(QualityCheckFailed):
        active.publish(candidate("v2"), [QualityCheck("rows", lambda payload: False)])

    assert active.current("screener").version == "v1"


def test_passing_candidate_replaces_current_snapshot(publisher) -> None:
    active, factory = publisher
    active.publish(candidate("v1"), [])
    active.publish(candidate("v2"), [])

    with factory() as session:
        rows = session.scalars(select(PublishedSnapshot)).all()

    assert len(rows) == 1
    assert rows[0].version == "v2"


def test_older_release_is_retained_without_replacing_newer_current_pointer(
    publisher,
) -> None:
    active, factory = publisher
    newer = dated_mart_candidate("screener", 14, "newer")
    older = dated_mart_candidate("screener", 12, "older")
    active.publish_many((newer,), "v-newer", passing_results())

    published = active.publish_many((older,), "v-older", passing_results())

    assert published[0].version == "v-newer"
    assert published[0].as_of.date().isoformat() == "2026-09-14"
    with factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version))) == {
            "v-newer",
            "v-older",
        }
    assert active.current("screener").version == "v-newer"


def test_same_date_release_may_replace_current_pointer(publisher) -> None:
    active, _factory = publisher
    active.publish_many(
        (dated_mart_candidate("screener", 14, "first"),),
        "v-first",
        passing_results(),
    )

    published = active.publish_many(
        (dated_mart_candidate("screener", 14, "forced"),),
        "v-forced",
        passing_results(),
    )

    assert published[0].version == "v-forced"
    assert active.current("screener").version == "v-forced"


def test_same_day_older_instant_cannot_demote_newer_macro_pointer(publisher) -> None:
    active, _ = publisher
    newer = MartCandidate("macro-insights", datetime(2026, 9, 20, 10, tzinfo=timezone.utc), {"version": "new"})
    older = MartCandidate("macro-insights", datetime(2026, 9, 20, 9, tzinfo=timezone.utc), {"version": "old"})
    active.publish_many((newer,), "new", passing_results())
    outcome = active.publish_many((older,), "old", passing_results())
    assert outcome.promoted_products == ()
    assert active.current("macro-insights").version == "new"
    assert active.current("macro-insights").as_of == datetime(2026, 9, 20, 10)


def test_mixed_macro_pointer_times_skip_both_promotions_in_one_release(publisher) -> None:
    active, factory = publisher
    day = datetime(2026, 9, 20, tzinfo=timezone.utc)
    transmission = "macro-transmission"
    insights = "macro-insights"
    active.publish_many((MartCandidate(transmission, day + timedelta(hours=10), {}),), "transmission-new", passing_results())
    active.publish_many((MartCandidate(insights, day + timedelta(hours=8), {}),), "insights-old", passing_results())

    pair = (
        MartCandidate(transmission, day + timedelta(hours=9), {"version": "replay"}),
        MartCandidate(insights, day + timedelta(hours=9), {"version": "replay"}),
    )
    outcome = active.publish_many(pair, "pair-replay", passing_results())

    assert outcome.promoted_products == ()
    assert active.current(transmission).version == "transmission-new"
    assert active.current(insights).version == "insights-old"
    with factory() as session:
        history = session.scalars(select(PageSnapshot).where(PageSnapshot.snapshot_version == "pair-replay")).all()
    assert {item.product for item in history} == {transmission, insights}


def test_macro_stale_metadata_is_written_and_cleared(publisher) -> None:
    active, _ = publisher
    when = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    stale = MartCandidate("macro-insights", when, {}, stale=True, stale_reason="missing=1")
    active.publish_many((stale,), "stale", passing_results())
    assert active.current("macro-insights").stale is True
    assert active.current("macro-insights").stale_reason == "missing=1"
    active.publish_many((MartCandidate("macro-insights", when + timedelta(hours=1), {}),), "fresh", passing_results())
    assert active.current("macro-insights").stale is False
    assert active.current("macro-insights").stale_reason is None


def test_second_macro_write_failure_rolls_back_both_pointers(publisher, monkeypatch) -> None:
    active, factory = publisher
    old = (mart_candidate("macro-transmission"), mart_candidate("macro-insights"))
    active.publish_many(old, "old", passing_results())
    original = active._write_page_snapshot

    def fail_second(session, candidate, version):
        if candidate.product == "macro-insights":
            raise RuntimeError("second write")
        return original(session, candidate, version)

    monkeypatch.setattr(active, "_write_page_snapshot", fail_second)
    with pytest.raises(RuntimeError, match="second write"):
        active.publish_many(old, "new", passing_results())
    assert active.current("macro-transmission").version == "old"
    assert active.current("macro-insights").version == "old"
    with factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version))) == {"old"}


def test_newer_release_advances_current_pointer(publisher) -> None:
    active, _factory = publisher
    active.publish_many(
        (dated_mart_candidate("screener", 12, "older"),),
        "v-older",
        passing_results(),
    )

    published = active.publish_many(
        (dated_mart_candidate("screener", 14, "newer"),),
        "v-newer",
        passing_results(),
    )

    assert published[0].version == "v-newer"
    assert active.current("screener").version == "v-newer"


def test_same_version_replay_does_not_claim_new_promotion(publisher) -> None:
    active, _factory = publisher
    first = active.publish_many((mart_candidate("screener"),), "v1", passing_results())
    replay = active.publish_many((mart_candidate("screener"),), "v1", passing_results())

    assert first.promoted_products == ("screener",)
    assert replay[0].version == "v1"
    assert replay.promoted_products == ()


def test_same_version_replay_rejects_changed_instant_and_rolls_back(publisher) -> None:
    active, factory = publisher
    original = MartCandidate(
        product="screener",
        as_of=datetime(2026, 9, 14, 10, tzinfo=timezone.utc),
        payload={"label": "stable"},
    )
    equivalent = MartCandidate(
        product="screener",
        as_of=datetime(2026, 9, 14, 18, tzinfo=timezone(timedelta(hours=8))),
        payload={"label": "stable"},
    )
    changed = MartCandidate(
        product="screener",
        as_of=datetime(2026, 9, 14, 11, tzinfo=timezone.utc),
        payload={"label": "stable"},
    )
    active.publish_many((original,), "v-stable", ())
    active.publish_many((equivalent,), "v-stable", ())

    with pytest.raises(ValueError, match="as_of"):
        active.publish_many(
            (mart_candidate("other-product"), changed), "v-stable", ()
        )

    with factory() as session:
        history = session.scalar(select(PageSnapshot).where(
            PageSnapshot.product == "screener",
            PageSnapshot.snapshot_version == "v-stable",
        ))
        assert history is not None
        assert history.as_of == datetime(2026, 9, 14, 10)
        assert session.scalar(select(PageSnapshot).where(
            PageSnapshot.product == "other-product"
        )) is None
    assert active.current("other-product") is None
    assert active.current("screener").as_of == datetime(2026, 9, 14, 10)


def test_publish_locks_existing_pointer_before_resetting_freshness(
    publisher,
) -> None:
    active, factory = publisher
    statements: list[str] = []

    def record_pointer_select(execute_state: ORMExecuteState) -> None:
        if not execute_state.is_select:
            return
        statement = str(
            execute_state.statement.compile(dialect=postgresql.dialect())
        )
        if "published_snapshot" in statement:
            statements.append(statement)

    event.listen(factory.class_, "do_orm_execute", record_pointer_select)
    try:
        active.publish(candidate("v1"), [])
        statements.clear()

        active.publish(candidate("v2"), [])
    finally:
        event.remove(factory.class_, "do_orm_execute", record_pointer_select)

    assert len(statements) == 1
    assert "FOR UPDATE" in statements[0]


def test_publish_clears_stale_reason_and_records_freshness_check(publisher) -> None:
    active, factory = publisher
    previous_check = datetime(2026, 8, 16, tzinfo=timezone.utc)
    with factory.begin() as session:
        session.add(
            PublishedSnapshot(
                product="screener",
                version="v1",
                as_of=datetime(2026, 8, 16, tzinfo=timezone.utc),
                payload={"version": "v1"},
                stale=True,
                stale_reason="SNAPSHOT_BEHIND_EXPECTED_DATE",
                freshness_checked_at=previous_check,
                published_at=previous_check,
            )
        )

    active.publish(candidate("v2"), [])

    refreshed = active.current("screener")
    assert refreshed is not None
    assert refreshed.stale is False
    assert refreshed.stale_reason is None
    assert refreshed.freshness_checked_at == refreshed.published_at
    assert refreshed.freshness_checked_at != previous_check.replace(tzinfo=None)


def test_publish_many_writes_history_and_switches_all_products(publisher) -> None:
    active, factory = publisher

    published = active.publish_many(v1_candidates(), "v1", passing_results())

    assert {row.product for row in published} == {"screener", "portfolio"}
    with factory() as session:
        assert session.scalar(select(func.count(PageSnapshot.id))) == 2
    assert active.current("screener").version == "v1"
    assert active.current("portfolio").version == "v1"


def test_publish_many_failure_keeps_every_previous_pointer(publisher, monkeypatch) -> None:
    active, _factory = publisher
    active.publish_many(v1_candidates(), "v1", passing_results())

    def fail_on_portfolio(session, candidate, version) -> None:
        if candidate.product == "portfolio":
            raise RuntimeError("portfolio write failed")

    monkeypatch.setattr(active, "_write_page_snapshot", fail_on_portfolio)
    with pytest.raises(RuntimeError):
        active.publish_many(v2_candidates(), "v2", passing_results())

    assert active.current("screener").version == "v1"
    assert active.current("portfolio").version == "v1"


def test_structured_rows_are_prefetched_once_per_table(publisher) -> None:
    active, factory = publisher
    rows = MartRows(
        screener=tuple({"symbol": f"00000{index}.SZ"} for index in range(3)),
        workbenches=tuple({"symbol": f"00000{index}.SZ"} for index in range(3)),
        charts=tuple(
            {"date": f"2026-08-{17 + index:02d}", "close": index + 1}
            for index in range(3)
        ),
    )
    candidate = MartCandidate(
        product="stock-workbench:600519.SH",
        as_of=datetime(2026, 8, 19, tzinfo=timezone.utc),
        payload={"symbol": "600519.SH"},
        rows=rows,
    )
    active.publish_many((candidate,), "v1", passing_results())
    statements: list[str] = []
    engine = factory.kw["bind"]

    @event.listens_for(engine, "before_cursor_execute")
    def record_selects(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    active.publish_many((candidate,), "v1", passing_results())

    assert sum("stock_screener_daily" in sql for sql in statements) == 1
    assert sum("stock_workbench_snapshot" in sql for sql in statements) == 1
    assert sum("stock_daily_chart" in sql for sql in statements) == 1
