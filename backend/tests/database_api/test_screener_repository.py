from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, DerivedBase, MartBase
from app.db.models import PublishedSnapshot, StockScreenerDaily
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider


def test_database_provider_reads_screener_rows_when_snapshot_pointer_is_missing() -> None:
    engine = create_engine("sqlite:///:memory:", execution_options={"schema_translate_map": {"raw": None, "core": None, "ops": None, "derived": None, "mart": None}})
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(StockScreenerDaily(ts_code="600000.SH", as_of=date(2026, 8, 17), snapshot_version="v1", fields={"symbol": "600000.SH", "pe": None}))
    provider = StocksDatabaseProvider(SnapshotRepository(factory), factory)
    result = provider.read_screener_universe()
    assert result.raw["stocks"][0]["symbol"] == "600000.SH"
    assert result.meta.source == "database"
