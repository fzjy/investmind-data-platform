from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import JobCursor, JobRun, SourcePayload
from app.ingestion.repositories import content_hash
from app.market.data.catalog import MarketDataCatalog
from app.shared.errors import DataServiceError
from backend.tests.market_data.test_foundation import client_for
from backend.tests.market_etf.test_etf import CODE, DAY, NOW


def legacy_proofs(factory, job, fingerprint, *, register_job=True):
    """复现旧版只有源编号和正文哈希的成功游标，指纹来自当时的作业记录。"""
    with factory.begin() as session:
        if register_job:
            session.add(JobRun(id=str(uuid4()), job_name=job, business_key=f"{fingerprint}:{DAY}", started_at=NOW,
                               finished_at=NOW, status="partial", details={}))
        for cursor in session.scalars(select(JobCursor).where(JobCursor.job_name == job)):
            raw = session.get(SourcePayload, cursor.cursor_value["sourcePayloadId"])
            raw.payload = {k:v for k,v in raw.payload.items() if k != "contractFingerprint"}
            raw.content_hash = content_hash(raw.payload)
            cursor.cursor_value = {"sourcePayloadId":raw.id, "contentHash":raw.content_hash}


def renamed(catalog):
    assets = tuple(a.model_copy(update={"name":a.name+"X"}) for a in reversed(catalog.assets))
    commodities = catalog.commodities.model_copy(update={"history_days":7,
        "assets":tuple(a.model_copy(update={"name":a.name+"X"}) for a in reversed(catalog.commodities.assets))})
    return catalog.model_copy(update={"assets":assets, "commodities":commodities})


@pytest.mark.parametrize("legacy", [False, True])
def test_etf_historical_fingerprint_survives_catalog_change(pg_factory, legacy):
    from app.market.etf.repository import EtfRepository, fingerprint, JOB_NAME
    from backend.tests.market_etf.test_etf import response
    catalog = MarketDataCatalog.load()
    fp = fingerprint(catalog)
    repo = EtfRepository(pg_factory)
    assert repo.persist(CODE, DAY, "fund_daily", fp, response("fund_daily"), NOW) is None
    if legacy:
        legacy_proofs(pg_factory, JOB_NAME, fp)
    changed = renamed(catalog)
    assert repo.component(CODE, DAY, "fund_daily", fingerprint(changed), NOW) is None
    with client_for(pg_factory, changed) as client:
        rows = client.get("/api/v1/datasets/etf-trading", params={"asset_code":CODE,"trade_date":DAY}).json()["data"]
        assert len(rows) == 1 and rows[0]["quote"]["amount"] > 0 and rows[0]["shares"] is None


def test_etf_simultaneous_inconsistent_captures_rejected(pg_factory):
    from app.market.etf.repository import EtfRepository, fingerprint
    from backend.tests.market_etf.test_etf import response
    catalog = MarketDataCatalog.load()
    for config, amount in ((catalog,10),(renamed(catalog),20)):
        assert EtfRepository(pg_factory).persist(CODE,DAY,"fund_daily",fingerprint(config),
                                                response("fund_daily",value=amount),NOW) is None
    with client_for(pg_factory) as client, pytest.raises(DataServiceError) as error:
        client.get("/api/v1/datasets/etf-trading", params={"asset_code":CODE,"trade_date":DAY})
    assert error.value.code == "INVALID_SOURCE_PAYLOAD"


def test_etf_fingerprint_must_bind_cursor_even_if_raw_checksum_matches(pg_factory):
    from app.market.etf.repository import EtfRepository, JOB_NAME, fingerprint
    from backend.tests.market_etf.test_etf import response
    fp = fingerprint(MarketDataCatalog.load())
    assert EtfRepository(pg_factory).persist(CODE,DAY,"fund_daily",fp,response("fund_daily"),NOW) is None
    with pg_factory.begin() as session:
        cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME))
        raw = session.get(SourcePayload,cursor.cursor_value["sourcePayloadId"])
        raw.payload = {**raw.payload,"contractFingerprint":"f"*64}
        raw.content_hash = content_hash(raw.payload)
        cursor.cursor_value = {**cursor.cursor_value,"contentHash":raw.content_hash}
    with client_for(pg_factory) as client:
        assert client.get("/api/v1/datasets/etf-trading",params={"asset_code":CODE,"trade_date":DAY}).json()["data"] == []


@pytest.mark.parametrize("legacy", [False, True])
def test_commodity_frozen_range_and_single_contract_group(new_pg_factory, legacy):
    from app.market.commodities.contracts import fingerprint, JOB_NAME
    from app.market.commodities.repository import CommodityRepository
    from backend.tests.market_commodities.test_sources import setup, complete, response
    _, catalog, factory = setup(new_pg_factory)
    plan = complete(factory, catalog)
    if legacy:
        legacy_proofs(factory, JOB_NAME, fingerprint(catalog))
    changed = renamed(catalog)
    params = {"asset_code":"AU.SHF","window_end":DAY,"start_date":DAY-timedelta(days=4),"end_date":DAY}
    with client_for(factory, changed) as client:
        assert len(client.get("/api/v1/datasets/commodity-daily",params=params).json()["data"]) == 5
        mapping = next(r for r in plan["requests"] if r["params"].get("ts_code") == "AU.SHF")
        with factory.begin() as session:
            session.delete(session.scalar(select(JobCursor).filter_by(cursor_key=mapping["identity"])))
        # 同起止而指纹不同的映射也不能补到缺件的旧包中。
        other = changed.model_copy(update={"commodities":changed.commodities.model_copy(update={"history_days":5})})
        from app.market.commodities.contracts import request
        alternate = request(mapping["api"],mapping["params"],other,DAY)
        assert CommodityRepository(factory,other).persist(alternate,response(alternate),NOW) is None
        assert client.get("/api/v1/datasets/commodity-daily",params=params).json()["data"] == []


def save_gold(factory, catalog, captured_at, *, amount):
    from app.connectors.tushare.contracts import TushareResponse
    from app.market.commodities.contracts import source_requests, request, window
    from app.market.commodities.repository import CommodityRepository
    from backend.tests.market_commodities.test_sources import response
    repo = CommodityRepository(factory,catalog)
    dates = {"start_date":window(catalog,DAY).strftime("%Y%m%d"), "end_date":DAY.strftime("%Y%m%d")}
    reqs = [r for r in source_requests(catalog,DAY) if r["params"].get("ts_code") == "AU.SHF" or
            r["params"].get("fut_code") == "AU" or (r["api"] == "fut_trade_cal" and r["params"]["exchange"] == "SHFE")]
    reqs.extend(request("fut_daily",{"ts_code":code,**dates},catalog,DAY) for code in ("AU2610.SHF","AU2612.SHF"))
    for req in reqs:
        sample = response(req)
        rows = [list(row) for row in sample.items]
        if req["api"] == "fut_daily":
            for row in rows:
                row[sample.fields.index("amount")] = amount
        if req["api"] != "fut_basic":
            for i in range(5, catalog.commodities.history_days):
                row = list(rows[0])
                row[1] = (DAY-timedelta(days=i)).strftime("%Y%m%d")
                rows.append(row)
        assert repo.persist(req, TushareResponse(fields=sample.fields, items=rows), captured_at) is None


def test_full_windows_keep_their_own_values_and_conflicts(pg_factory):
    catalog = MarketDataCatalog.load()
    short = catalog.model_copy(update={"commodities":catalog.commodities.model_copy(update={"history_days":5})})
    long = renamed(catalog)
    save_gold(pg_factory,long,NOW,amount=1)
    save_gold(pg_factory,short,NOW+timedelta(seconds=1),amount=2)
    params = {"asset_code":"AU.SHF","window_end":DAY,"end_date":DAY}
    with client_for(pg_factory) as client:
        def rows(days):
            return client.get("/api/v1/datasets/commodity-daily",
                              params={**params,"start_date":DAY-timedelta(days=days-1)}).json()["data"]
        older = rows(7)
        assert len(older) == 7 and {row["amount"] for row in older} == {1}
        newer = rows(5)
        assert len(newer) == 5 and {row["amount"] for row in newer} == {2}
        alternate = long.model_copy(update={"commodities":long.commodities.model_copy(update={"history_days":5})})
        save_gold(pg_factory,alternate,NOW+timedelta(seconds=1),amount=3)
        with pytest.raises(DataServiceError) as error:
            rows(5)
        assert error.value.code == "INVALID_SOURCE_PAYLOAD"


@pytest.mark.parametrize("other_job", [False, True])
def test_legacy_direct_write_can_reconstruct_raw_window_length(pg_factory, other_job):
    from app.market.commodities.contracts import JOB_NAME, fingerprint, source_requests
    from app.market.commodities.repository import CommodityRepository
    from backend.tests.market_commodities.test_sources import response
    catalog = MarketDataCatalog.load()
    old = catalog.model_copy(update={"commodities":catalog.commodities.model_copy(update={"history_days":5})})
    save_gold(pg_factory,old,NOW,amount=0)
    if other_job:
        other = renamed(old)
        mapping = next(r for r in source_requests(other,DAY) if r["params"].get("ts_code") == "AG.SHF")
        assert CommodityRepository(pg_factory,other).persist(mapping,response(mapping),NOW) is None
        legacy_proofs(pg_factory, JOB_NAME, fingerprint(other))
    else:
        legacy_proofs(pg_factory, JOB_NAME, fingerprint(old), register_job=False)
    with client_for(pg_factory) as client:
        rows = client.get("/api/v1/datasets/commodity-daily", params={
            "asset_code":"AU.SHF","window_end":DAY,"start_date":DAY-timedelta(days=4),"end_date":DAY}).json()["data"]
        assert len(rows) == 5 and all(r["amount"] == 0 for r in rows)


def test_etf_direct_write_alongside_another_job(pg_factory, monkeypatch):
    from app.market.etf.repository import EtfRepository, JOB_NAME, fingerprint
    from backend.tests.market_etf.test_etf import response
    catalog = MarketDataCatalog.load()
    other_fp = fingerprint(renamed(catalog))
    repo = EtfRepository(pg_factory)
    for code, fp, amount in ((CODE,fingerprint(catalog),7),("510050.SH",other_fp,19)):
        assert repo.persist(code,DAY,"fund_daily",fp,response("fund_daily",code=code,value=amount),NOW) is None
    legacy_proofs(pg_factory, JOB_NAME, other_fp)
    with client_for(pg_factory) as client:
        for code, expected in ((CODE,7),("510050.SH",19)):
            rows = client.get("/api/v1/datasets/etf-trading",params={"asset_code":code,"trade_date":DAY}).json()["data"]
            assert len(rows) == 1 and rows[0]["quote"]["amount"] == expected
        def unavailable_catalog():
            raise FileNotFoundError()
        monkeypatch.setattr(MarketDataCatalog, "load", unavailable_catalog)
        registered = client.get("/api/v1/datasets/etf-trading",params={"asset_code":"510050.SH","trade_date":DAY})
        assert registered.json()["data"][0]["quote"]["amount"] == 19
