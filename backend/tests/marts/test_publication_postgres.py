from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

from app.db.base import MartBase
from app.db.models import PageSnapshot, PublishedSnapshot
from app.marts.contracts import MartCandidate
from app.marts.publication import SnapshotPublisher


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL publication tests",
    ),
]


@pytest.fixture(scope="module")
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        engine = create_engine(postgres.get_connection_url(driver="psycopg"))
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE SCHEMA mart"))
            MartBase.metadata.create_all(engine)
            yield sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()


def _candidate(day: int, label: str) -> MartCandidate:
    return MartCandidate(
        product="screener",
        as_of=datetime(2026, 9, day, tzinfo=timezone.utc),
        payload={"label": label},
    )


def test_postgres_current_pointer_is_monotonic_while_history_is_retained(
    postgres_factory: sessionmaker[Session],
) -> None:
    publisher = SnapshotPublisher(postgres_factory)

    publisher.publish_many((_candidate(14, "newer"),), "v-14", ())
    stale = publisher.publish_many((_candidate(12, "stale"),), "v-12", ())

    assert stale[0].version == "v-14"
    assert stale[0].as_of.date().isoformat() == "2026-09-14"
    assert publisher.current("screener").version == "v-14"
    with postgres_factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version))) == {
            "v-12",
            "v-14",
        }

    same_day = publisher.publish_many(
        (_candidate(14, "same-day-force"),),
        "v-14-force",
        (),
    )
    assert same_day[0].version == "v-14-force"

    later = publisher.publish_many((_candidate(15, "later"),), "v-15", ())
    assert later[0].version == "v-15"
    assert publisher.current("screener").version == "v-15"


def test_postgres_failed_stale_release_rolls_back_history_and_all_pointers(
    postgres_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = SnapshotPublisher(postgres_factory)
    current = MartCandidate(
        product="atomic-screener",
        as_of=datetime(2026, 9, 14, tzinfo=timezone.utc),
        payload={"label": "current"},
    )
    stale = MartCandidate(
        product="atomic-screener",
        as_of=datetime(2026, 9, 12, tzinfo=timezone.utc),
        payload={"label": "stale"},
    )
    publisher.publish_many(
        (current,), "v-current", ()
    )
    original_write = publisher._write_page_snapshot

    def fail_second_product(session: Session, candidate: MartCandidate, version: str) -> None:
        if candidate.product == "portfolio":
            raise RuntimeError("second product failed")
        original_write(session, candidate, version)

    monkeypatch.setattr(publisher, "_write_page_snapshot", fail_second_product)
    portfolio = MartCandidate(
        product="portfolio",
        as_of=datetime(2026, 9, 12, tzinfo=timezone.utc),
        payload={"label": "portfolio"},
    )

    with pytest.raises(RuntimeError, match="second product failed"):
        publisher.publish_many((stale, portfolio), "v-aborted", ())

    with postgres_factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version).where(
            PageSnapshot.product == "atomic-screener"
        ))) == {
            "v-current",
        }
    assert publisher.current("atomic-screener").version == "v-current"
    assert publisher.current("portfolio") is None


def test_postgres_pointer_compares_utc_dates_under_los_angeles_session(
    postgres_factory: sessionmaker[Session],
) -> None:
    la_engine = create_engine(
        postgres_factory.kw["bind"].url,
        connect_args={"options": "-c timezone=America/Los_Angeles"},
    )
    try:
        la_factory = sessionmaker(la_engine, expire_on_commit=False)
        with la_factory() as session:
            assert session.scalar(text("SHOW TIME ZONE")) == "America/Los_Angeles"
        publisher = SnapshotPublisher(la_factory)
        current = MartCandidate(
            product="utc-date-boundary",
            as_of=datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc),
            payload={"label": "current"},
        )
        older = MartCandidate(
            product="utc-date-boundary",
            as_of=datetime(2026, 9, 13, 23, 30, tzinfo=timezone.utc),
            payload={"label": "older"},
        )

        publisher.publish_many((current,), "v-utc-14", ())
        result = publisher.publish_many((older,), "v-utc-13", ())

        assert result[0].version == "v-utc-14"
        assert publisher.current("utc-date-boundary").version == "v-utc-14"
        with la_factory() as session:
            assert set(session.scalars(select(PageSnapshot.snapshot_version).where(
                PageSnapshot.product == "utc-date-boundary"
            ))) == {"v-utc-13", "v-utc-14"}
    finally:
        la_engine.dispose()


def test_naive_candidates_store_utc_instants_and_keep_same_day_promotion_in_la(
    postgres_factory: sessionmaker[Session],
) -> None:
    la_engine = create_engine(
        postgres_factory.kw["bind"].url,
        connect_args={"options": "-c timezone=America/Los_Angeles"},
    )
    try:
        la_factory = sessionmaker(la_engine, expire_on_commit=False)
        with la_factory() as session:
            assert session.scalar(text("SHOW TIME ZONE")) == "America/Los_Angeles"
        publisher = SnapshotPublisher(la_factory)
        late = MartCandidate(
            product="naive-utc-boundary",
            as_of=datetime(2026, 9, 14, 23, 30),
            payload={"label": "late"},
        )
        early = MartCandidate(
            product="naive-utc-boundary",
            as_of=datetime(2026, 9, 14, 0, 30),
            payload={"label": "early"},
        )

        publisher.publish_many((late,), "v-late", ())
        result = publisher.publish_many((early,), "v-early", ())

        assert result[0].version == "v-early"
        assert result.promoted_products == ("naive-utc-boundary",)
        with la_factory() as session:
            page_instants = dict(session.execute(text(
                "SELECT snapshot_version, as_of AT TIME ZONE 'UTC' "
                "FROM mart.page_snapshot WHERE product = 'naive-utc-boundary'"
            )).all())
            current_instant = session.scalar(select(PublishedSnapshot.as_of).where(
                PublishedSnapshot.product == "naive-utc-boundary"
            ))
        assert page_instants == {
            "v-late": datetime(2026, 9, 14, 23, 30),
            "v-early": datetime(2026, 9, 14, 0, 30),
        }
        assert current_instant is not None
        assert current_instant.astimezone(timezone.utc) == datetime(
            2026, 9, 14, 0, 30, tzinfo=timezone.utc
        )
    finally:
        la_engine.dispose()


def test_postgres_same_version_as_of_is_immutable_in_la_session(
    postgres_factory: sessionmaker[Session],
) -> None:
    la_engine = create_engine(
        postgres_factory.kw["bind"].url,
        connect_args={"options": "-c timezone=America/Los_Angeles"},
    )
    try:
        la_factory = sessionmaker(la_engine, expire_on_commit=False)
        with la_factory() as session:
            assert session.scalar(text("SHOW TIME ZONE")) == "America/Los_Angeles"
        publisher = SnapshotPublisher(la_factory)
        def replay(as_of: datetime) -> MartCandidate:
            return MartCandidate(
                product="immutable-as-of",
                as_of=as_of,
                payload={"label": "stable"},
            )

        publisher.publish_many((replay(datetime(2026, 9, 14, 10, tzinfo=timezone.utc)),), "v1", ())
        publisher.publish_many((replay(datetime(2026, 9, 14, 18, tzinfo=timezone(timedelta(hours=8)))),), "v1", ())
        with pytest.raises(ValueError, match="as_of"):
            publisher.publish_many((
                MartCandidate(
                    product="rollback-witness",
                    as_of=datetime(2026, 9, 14, tzinfo=timezone.utc),
                    payload={"label": "witness"},
                ),
                replay(datetime(2026, 9, 14, 11, tzinfo=timezone.utc)),
            ), "v1", ())

        with la_factory() as session:
            history = session.scalar(select(PageSnapshot).where(
                PageSnapshot.product == "immutable-as-of",
            ))
            current = session.get(PublishedSnapshot, "immutable-as-of")
            assert history is not None and current is not None
            assert history.as_of.astimezone(timezone.utc) == datetime(
                2026, 9, 14, 10, tzinfo=timezone.utc
            )
            assert current.as_of.astimezone(timezone.utc) == history.as_of.astimezone(timezone.utc)
            assert session.get(PublishedSnapshot, "rollback-witness") is None
            assert session.scalar(select(PageSnapshot).where(
                PageSnapshot.product == "rollback-witness"
            )) is None
    finally:
        la_engine.dispose()
