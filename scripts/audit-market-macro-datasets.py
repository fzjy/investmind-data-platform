"""在同一只读事务从公共 GET、独立 SQL 和已发布产品三个入口复核市场与宏观。"""
from collections import Counter
import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from app.config import settings
from app.db.models import PublishedSnapshot
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.datasets_routes import create_router as macro_router
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.market.commodities.repository import package
from app.market.commodities.dto import valid_commodity_payload
from app.market.data.catalog import MarketDataCatalog
from app.market.datasets_routes import create_router as market_router
from app.market.etf.repository import tiles
from app.shared.readonly_database import open_readonly_factory


def wire(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def read(client, dataset, **params):
    rows = []
    while True:
        response = client.get("/api/v1/datasets/"+dataset, params=params)
        response.raise_for_status()
        page = json.loads(response.content, parse_float=Decimal)
        rows.extend(page["data"])
        if page["meta"]["next_cursor"] is None:
            return rows
        params["cursor"] = page["meta"]["next_cursor"]


def markets(session, client, altered_client, checked_at):
    quotes = session.execute(text("""
        select provider,api_name,asset_type,asset_code,trade_date,open,high,low,close,pre_close,pct_chg,
               source_payload_id,captured_at
        from core.market_daily_quote where provider='tushare' order by asset_code,trade_date
    """)).mappings().all()
    by_code = {}
    for row in quotes:
        by_code.setdefault((row["asset_type"], row["asset_code"]), []).append(row)
    for (kind, code), expected in by_code.items():
        result = read(client, "market-daily", asset_type=kind, asset_code=code,
                      start_date=expected[0]["trade_date"], end_date=expected[-1]["trade_date"], limit=50)
        assert len(result) == len(expected), code
        for actual, reference in zip(result, expected, strict=True):
            for key, value in reference.items():
                assert actual[key] == wire(value), (code, key)
    explorer = session.get(PublishedSnapshot, "market-explorer")
    payload = explorer.payload
    commodity_counts = {}
    etfs = payload.get("heatmapData", {}).get("etf", [])
    for tile in etfs:
        etf = tile["etf"]
        row, = read(client, "etf-trading", asset_code=etf["code"], trade_date=etf["tradeDate"])
        assert [row] == read(altered_client, "etf-trading", asset_code=etf["code"], trade_date=etf["tradeDate"])
        assert row["quote"]["source_payload_id"] == etf["sourcePayloadIds"]["quote"]
        assert row["shares"]["source_payload_id"] == etf["sourcePayloadIds"]["shares"]
        for component, key, expected in (("quote", "close", etf["close"]), ("quote", "vol", etf["volume"]),
                                         ("shares", "fd_share", etf["shares"])):
            assert float(row[component][key]) == expected
        assert float(row["quote"]["amount"] / 100000) == tile["turnover"]
    fields = {"pre_close":"preClose","pre_settle":"preSettle","oi":"openInterest","vol":"volume",
              **{k:k for k in ("open","high","low","close","amount","settle","contract")}}
    for group in payload.get("commodityData", {}).values():
        for instrument in group["domestic"]:
            meta = payload["commodityMetadata"]
            rows = read(client, "commodity-daily", asset_code=instrument["code"], window_end=meta["asOf"],
                        start_date=meta["windowStart"], end_date=meta["asOf"])
            assert rows == read(altered_client, "commodity-daily", asset_code=instrument["code"], window_end=meta["asOf"],
                                start_date=meta["windowStart"], end_date=meta["asOf"])
            for row, bar in zip(rows, instrument["bars"]["daily"], strict=True):
                assert row["trade_date"] == bar["date"] and row["quote_unit"] == instrument["quoteUnit"]
                for key, product_key in fields.items():
                    assert (float(row[key]) if key != "contract" else row[key]) == bar[product_key]
                assert {s["source_payload_id"] for s in row["sources"].values()} <= set(instrument["sourcePayloadIds"])
            commodity_counts[instrument["code"]] = len(rows)
    catalog = MarketDataCatalog.load()
    change_basis = Counter()
    if etfs:
        assert tiles(session, catalog, date.fromisoformat(etfs[0]["etf"]["tradeDate"]), checked_at) == etfs
    if commodity_counts:
        product = package(session, catalog, date.fromisoformat(payload["commodityMetadata"]["asOf"]), checked_at)
        assert product and valid_commodity_payload(product) and valid_commodity_payload(payload)
        for category, group in product["commodityData"].items():
            for current, saved in zip(group["domestic"], payload["commodityData"][category]["domestic"], strict=True):
                if current["changeBasis"] != saved["changeBasis"]:
                    assert saved["changeBasis"] == "close/pre_settle-1"
                    assert current["changeBasis"] == "close/previous_series_close-1"
                    change_basis["verifiedLegacyVsCurrentFormula"] += 1
                    # 上一步已按两套各自的合同复算涨幅，余下字段仍须逐一相等。
                    current.update(changeBasis=saved["changeBasis"], change=saved["change"])
        if payload["commodityPeriods"] != product["commodityPeriods"]:
            assert payload["commodityPeriods"][0]["id"] == "intraday"
            assert payload["commodityPeriods"][1:] == product["commodityPeriods"]
            assert all(row["bars"]["intraday"] == [] for group in payload["commodityData"].values()
                       for row in group["domestic"])
            change_basis["legacyIntradaySelectorWithEmptyBars"] += 1
            product["commodityPeriods"] = payload["commodityPeriods"]
        assert all(payload[key] == value for key, value in product.items())
    counts = session.execute(text("""
        select count(*) filter(where data->>'limit_status' is not null) as stored,
               count(*) filter(where data->>'_limitStatusLineage' is not null) as marked from core.daily_basic
    """)).mappings().one()
    statuses = session.execute(text("""
        select distinct on (data->>'limit_status', data->>'_limitStatusLineage' is not null)
               ts_code,trade_date,data->>'limit_status' as code,data->'_limitStatusLineage' as proof
        from core.daily_basic where data->>'limit_status' ~ '^[0-6]$'
        order by data->>'limit_status',data->>'_limitStatusLineage' is not null,trade_date,ts_code
    """)).mappings().all()
    for reference in statuses:
        row, = read(client, "stock-limit-status", symbol=reference["ts_code"], trade_date=reference["trade_date"])
        assert row["limit_status"] == int(reference["code"])
        assert row["source_payload_id"] == (reference["proof"] or {}).get("sourcePayloadId")
    raw_counts = session.execute(text("""
        select api_name,count(*) from raw.source_payload where api_name=any(cast(:apis as text[]))
        group by api_name order by api_name
    """), {"apis":"daily_info etf_basic fund_daily fund_share fut_basic fut_daily fut_mapping fut_trade_cal fx_daily index_basic index_daily index_global index_weight margin sz_daily_info".split()})
    return {"quoteRows":len(quotes), "quoteAssets":len(by_code), "etfTiles":len(etfs), "commodityBars":commodity_counts,
            "catalogRenameReorderAndWindowChange":{"identicalEtfs":len(etfs), "identicalCommodityDays":sum(commodity_counts.values())},
            "rawLimitStatus":dict(counts), "sampledStatuses":len(statuses),
            "catalogRawCounts":dict(raw_counts.all()), "changeBasis":dict(change_basis), "explorerVersion":explorer.version}


MACRO_SQL = """
with revisions as (
    select distinct on (observed_at,source) *
    from core.macro_observation
    where series_id=:series and source=any(cast(:providers as text[]))
      and observed_at between :start and :end and released_at<=:cutoff
    order by observed_at,source,released_at desc,revision desc
)
select distinct on (observed_at) series_id,source,name,region,frequency,unit,seasonal_adjustment,observed_at,
       released_at,revision,value,source_url
from revisions where valid_until is null or valid_until>:cutoff
order by observed_at,array_position(cast(:providers as text[]),source)
"""


def macro(factory, session, client):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    published = session.get(PublishedSnapshot, "macro-insights")
    cutoff = published.as_of
    points = {r["id"]: r["values"] for r in published.payload["sandboxIndicators"]}
    def check(series, start, end, instant):
        providers = [r.provider for r in sorted(series.sources, key=lambda r: r.priority)]
        expected = session.execute(text(MACRO_SQL), {"series":series.id, "providers":providers,
            "start":start, "end":min(end, instant.date()), "cutoff":instant}).mappings().all()
        actual = read(client, "macro-observations", series_id=series.id, start_date=start, end_date=end,
                      as_of=instant.isoformat())
        for row, reference in zip(actual, expected, strict=True):
            assert row == {k:wire(v) for k,v in reference.items()}, (series.id, row["observed_at"])
        return actual
    counts = {}
    for series in catalog.series:
        rows = check(series, date(1,1,1), cutoff.date(), cutoff)
        assert [(p["month"], p["value"]) for p in points[series.id]] == [
            (r["observed_at"],float(r["value"])) for r in rows]
        counts[series.id] = len(rows)
    # 实际生产构建器只生成内存候选，不调用发布器。两项产品各核对本次调整的观测投影。
    shadow = MacroMartBuilder(MacroRepository(factory), catalog=catalog).build(cutoff, published.version)
    checked_fields = {"macro-transmission":["transmission","analysis"],
                      "macro-insights":["sandboxIndicators","trendDefinitions","sandboxMonths"]}
    for candidate in shadow:
        previous = session.get(PublishedSnapshot, candidate.product)
        for field in checked_fields[candidate.product]:
            assert candidate.payload[field] == previous.payload[field], (candidate.product, field)
    changes = session.execute(text("""
        select series_id,observed_at,source,max(released_at) as released
        from core.macro_observation where series_id=any(cast(:series as text[]))
        group by series_id,observed_at,source having count(distinct released_at)>1 and count(distinct value)>1
        order by released desc,series_id,observed_at
    """), {"series":list(counts)}).mappings().all()
    replay = None
    for change in changes:
        series = catalog.require(change["series_id"])
        period, released = change["observed_at"], change["released"]
        cutoffs = [released-timedelta(microseconds=1), released]
        pair = [check(series, period, period, instant) for instant in cutoffs]
        if all(pair) and pair[0][0]["value"] != pair[1][0]["value"]:
            replay = {"series":series.id, "period":str(period), "versions":[
                {k:row[0][k] for k in ("value","source","released_at","revision")} for row in pair]}
            break
    specialties = {}
    for group in ("nominalGdp", "usdcnh"):
        source = published.payload["extensionInsights"]["sources"][group]
        rows = read(client, "macro-specialty-observations", series_id=source["id"], start_date="1900-01-01",
                    end_date=cutoff.date(), as_of=cutoff.isoformat(), limit=10)
        for row, point in zip(rows, source["points"], strict=True):
            assert (row["period"], row["value"], row["source_row"]) == (point["period"], Decimal(point["value"]), point["sourceRow"])
            assert row["retrieved_at"] == source["retrievedAt"] and row["evidence_fingerprint"] == source["evidenceFingerprint"]
        specialties[source["id"]] = len(rows)
    empty_series = {}
    for series_id, count in counts.items():
        if count == 0:
            rows = session.execute(text("""select source,count(*) from core.macro_observation where series_id=:series
                group by source order by source"""), {"series":series_id}).all()
            empty_series[series_id] = {"allStoredBySource":dict(rows),
                                      "configuredProviders":[r.provider for r in catalog.require(series_id).sources]}
    return {"version":published.version, "cutoff":cutoff.isoformat(), "selectedCounts":counts, "specialties":specialties,
            "emptySeries":empty_series,
            "shadowProductFields":checked_fields,
            "changedPeriodsWithinSource":len(changes), "revisionReplay":replay}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        sql_counts = Counter()
        def count(_conn, _cursor, statement, *_args):
            sql_counts[statement.lstrip().split()[0].upper()] += 1
        event.listen(session.connection(), "before_cursor_execute", count)
        app = FastAPI()
        app.include_router(market_router(factory))
        app.include_router(macro_router(factory))
        catalog = MarketDataCatalog.load()
        def renamed(assets):
            return tuple(a.model_copy(update={"name":a.name+"-audit"}) for a in reversed(assets))
        options = catalog.commodities.model_copy(update={"assets":renamed(catalog.commodities.assets),
            "history_days": 6 if catalog.commodities.history_days == 5 else 5})
        altered = FastAPI()
        altered.include_router(market_router(factory, catalog=catalog.model_copy(
            update={"assets":renamed(catalog.assets),"commodities":options})))
        started = perf_counter()
        with TestClient(app) as client, TestClient(altered) as altered_client:
            costs = {}
            def start_call(request):
                request.extensions["audit"] = perf_counter(), sum(sql_counts.values())
            def finish_call(response):
                start, before = response.request.extensions["audit"]
                stats = costs.setdefault(response.request.url.path.rsplit("/",1)[1],
                                         {"calls":0, "maxSql":0, "maxMs":0})
                stats["calls"] += 1
                stats["maxSql"] = max(stats["maxSql"], sum(sql_counts.values()) - before)
                stats["maxMs"] = max(stats["maxMs"], round(1000*(perf_counter()-start),1))
            client.event_hooks = {"request":[start_call],"response":[finish_call]}
            checked_at = session.scalar(text("select now()"))
            result = {"checkedAt":checked_at.isoformat(), "markets":markets(session, client, altered_client, checked_at),
                      "macro":macro(factory, session, client), "requests":costs}
        assert set(sql_counts) <= {"SELECT", "WITH"}, sql_counts
        result.update(sqlCounts=sql_counts, seconds=round(perf_counter()-started,3))
        report = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output is None:
        print(report)
    else:
        args.output.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
