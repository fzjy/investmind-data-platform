from datetime import date


from app.db.models import StockScreenerDaily
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider


def test_database_provider_reads_screener_rows_when_snapshot_pointer_is_missing(pg_factory) -> None:
    factory = pg_factory
    with factory.begin() as session:
        for symbol, version in [("600000.SH", "v1"), ("000001.SZ", "v1"), ("600000.SH", "v2")]:
            session.add(StockScreenerDaily(ts_code=symbol, as_of=date(2026, 8, 17), snapshot_version=version,
                                           fields={"symbol": symbol, "pe": None}))
    provider = StocksDatabaseProvider(SnapshotRepository(factory), factory)
    result = provider.read_screener_universe()
    assert result.raw["stocks"] == [{"symbol": "600000.SH", "pe": None}]
    assert result.meta.version == "v2"
    assert result.meta.source == "database"
