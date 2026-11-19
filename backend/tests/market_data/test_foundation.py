from datetime import timedelta
from decimal import Decimal
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.tests.market_data.test_repository import NOW, response, target


def client_for(factory, catalog=None):
    from app.market.datasets_routes import create_router
    app = FastAPI()
    app.include_router(create_router(factory, catalog=catalog))
    return TestClient(app)


def test_index_query_uses_trading_date_and_source(pg_factory):
    from app.market.data.repository import MarketQuoteRepository
    repo = MarketQuoteRepository(pg_factory)
    repo.persist(target(), response(), captured_at=NOW)
    params = dict(asset_type="indices", asset_code="000001.SH", start_date="2026-10-14", end_date="2026-10-15")
    with client_for(pg_factory) as client:
        url = "/api/v1/datasets/market-daily"
        rows = client.get(url, params=params).json()["data"]
        assert len(rows) == 1
        assert rows[0]["trade_date"] == "2026-10-15"
        assert rows[0]["source_payload_id"] == repo.latest(target(), checked_at=NOW).source_payload_id
        assert rows[0]["price_unit"] == "index_point"
        assert rows[0]["close"] == 100 and rows[0]["pct_chg"] == 0
        assert client.get(url, params={**params, "asset_type": "etfs"}).json()["data"] == []


def test_duplicate_and_cross_asset_cursor_are_rejected(pg_factory):
    from app.market.data.repository import MarketQuoteRepository
    from app.shared.errors import DataServiceError
    from app.connectors.tushare.contracts import TushareResponse
    a, b = response(day="20261014"), response(day="20261015")
    MarketQuoteRepository(pg_factory).persist(target(), TushareResponse(fields=a.fields, items=a.items+b.items),
                                              captured_at=NOW)
    params = dict(asset_type="indices", asset_code="000001.SH", start_date="2026-10-14", end_date="2026-10-15", limit=1)
    with client_for(pg_factory) as client:
        url = "/api/v1/datasets/market-daily"
        first = client.get(url, params=params).json()
        assert [r["trade_date"] for r in first["data"]] == ["2026-10-14"]
        later = {**params, "cursor":first["meta"]["next_cursor"]}
        assert client.get(url, params=later).json()["data"][0]["trade_date"] == "2026-10-15"
        with pytest.raises(DataServiceError, match="分页游标"):
            client.get(url, params={**later, "asset_code":"HSI"})
        with pytest.raises(DataServiceError, match="只能传一次"):
            client.get(url, params=[*params.items(), ("limit",2)])


def test_etf_components_preserve_partial_coverage_and_zero(pg_factory):
    from app.market.data.catalog import MarketDataCatalog
    from app.market.etf.repository import EtfRepository, fingerprint
    from backend.tests.market_etf.test_etf import CODE, DAY, NOW, response
    params = dict(asset_code=CODE, trade_date=DAY.isoformat())
    repo = EtfRepository(pg_factory)
    repo.persist(CODE, DAY, "fund_daily", fingerprint(MarketDataCatalog.load()),
                 response("fund_daily", value=0), NOW)
    with client_for(pg_factory) as client:
        row, = client.get("/api/v1/datasets/etf-trading", params=params).json()["data"]
        assert row["quote"]["amount"] == 0 and row["quote"]["vol"] > 0
        assert row["shares"] is None


def test_limit_status_zero_missing_and_damaged_evidence(new_pg_factory):
    from app.db.models import SourcePayload
    from backend.tests.market_pulse.test_repository import CODE, DAY, NOW, response, setup
    _, factory, repo = setup(new_pg_factory)
    with client_for(factory) as client:
        def rows():
            return client.get("/api/v1/datasets/stock-limit-status",
                              params={"symbol": CODE, "trade_date": DAY.isoformat()}).json()["data"]
        assert rows() == []
        assert repo.persist(CODE, DAY, "fp", response(0), NOW)
        row, = rows()
        assert row["limit_status"] == 0 and row["source_payload_id"]
        with factory.begin() as session:
            session.get(SourcePayload, row["source_payload_id"]).content_hash = "broken"
        assert rows() == []


def test_commodity_query_uses_verified_single_asset_window(new_pg_factory):
    from backend.tests.market_commodities.test_sources import setup, complete, DAY
    from app.db.models import JobCursor
    _, catalog, factory = setup(new_pg_factory)
    plan = complete(factory, catalog)
    # 另一品种缺少其主力映射仍能查询黄金。
    unrelated = next(r for r in plan["requests"] if r["params"].get("ts_code") == "AG.SHF")
    with factory.begin() as session:
        session.delete(session.scalar(select(JobCursor).filter_by(cursor_key=unrelated["identity"])))
    params = dict(asset_code="AU.SHF", window_end=DAY.isoformat(),
                  start_date=(DAY-timedelta(days=4)).isoformat(), end_date=DAY.isoformat(), limit=2)
    with client_for(factory, catalog) as client:
        url = "/api/v1/datasets/commodity-daily"
        first = client.get(url, params=params).json()
        assert [r["contract"] for r in first["data"]] == ["AU2610.SHF"] * 2
        next_params = {**params, "cursor": first["meta"]["next_cursor"]}
        row = client.get(url, params=next_params).json()["data"][0]
        assert row["contract"] == "AU2612.SHF" and row["quote_unit"] == "人民币元/吨"
        assert Decimal(str(row["amount"])) == Decimal("10000.5") and row["vol"] == 0
        assert set(row["sources"]) == {"fut_mapping", "fut_trade_cal", "fut_basic", "fut_daily"}
