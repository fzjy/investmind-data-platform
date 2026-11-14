"""只读复核市场总览与市场浏览的已发布子产品、字段日期、来源和可见历史。"""

import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from math import isclose
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from app.config import settings
from app.db.models import ContentDocument, PublishedSnapshot, SourcePayload
from app.ingestion.repositories import content_hash
from app.market.database_provider import MarketDatabaseProvider
from app.market.routes import create_router
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.shared.stock_mapping import StockMappingPolicy


def near(first, second):
    if first is None or second is None:
        return first is second
    return isclose(float(first), float(second), rel_tol=1e-9, abs_tol=1e-8)


def day(string):
    return date.fromisoformat(str(string)).isoformat()


class SourceRows:
    """同一事务缓存发布行实际引用的供应商原件；逐页验证内容校验和。"""

    def __init__(self, session):
        self.session = session
        self.cache = {}

    def read(self, identifier):
        if identifier not in self.cache:
            raw = self.session.get(SourcePayload, identifier)
            assert raw and raw.content_hash == content_hash(raw.payload), identifier
            self.cache[identifier] = (raw.api_name, [dict(zip(raw.payload["fields"], item, strict=True))
                                                     for item in raw.payload["items"]])
        return self.cache[identifier]


def check_commodities(explorer, originals):
    result = {}
    for category, areas in explorer["commodityData"].items():
        category_record = {"overseasInstruments": len(areas["overseas"]), "instruments": {}}
        result[category] = category_record
        for instrument in areas["domestic"]:
            sources = defaultdict(list)
            for raw_id in instrument["sourcePayloadIds"]:
                api_name, rows = originals.read(raw_id)
                sources[api_name].extend(rows)
            prices = {(r["ts_code"], day(r["trade_date"])): r for r in sources["fut_daily"]}
            mappings = {day(r["trade_date"]): r["mapping_ts_code"] for r in sources["fut_mapping"]
                        if r["ts_code"] == instrument["code"]}
            open_days = {day(r["cal_date"]) for r in sources["fut_trade_cal"] if str(r["is_open"]) == "1"}
            daily = instrument["bars"]["daily"]
            assert set(mappings) == open_days == {b["date"] for b in daily}
            for bar in daily:
                contract = mappings[bar["date"]]
                assert bar["contract"] == contract
                raw = prices[contract, bar["date"]]
                for name, column in {"open":"open", "high":"high", "low":"low", "close":"close",
                        "volume":"vol", "amount":"amount", "openInterest":"oi", "preClose":"pre_close",
                        "preSettle":"pre_settle", "settle":"settle"}.items():
                    assert near(raw[column], bar[name]), (instrument["code"], bar["date"], name)
            latest = daily[-1]
            meta = next(r for r in sources["fut_basic"] if r["ts_code"] == instrument["activeContract"])
            assert latest["contract"] == meta["ts_code"] and latest["date"] == instrument["tradeDate"]
            assert instrument["quoteUnit"] == meta["quote_unit"] and near(instrument["perUnit"], meta["per_unit"])
            base = latest["preSettle"] if instrument["changeBasis"] == "close/pre_settle-1" else daily[-2]["close"]
            assert near(instrument["price"], latest["close"]) and near(instrument["change"], (latest["close"]/base-1)*100)
            periods = {}
            for period in ("intraday", "daily", "weekly", "monthly"):
                bars = instrument["bars"][period]
                periods[period] = {"bars": len(bars), "from": bars[0]["date"] if bars else None,
                                   "to": bars[-1]["date"] if bars else None}
                if period not in ("weekly", "monthly"):
                    continue
                observed_days = []
                for bar in bars:
                    group = [d for d in daily if bar["periodStart"] <= d["date"] <= bar["periodEnd"]]
                    observed_days.extend(d["date"] for d in group)
                    assert len(group) == bar["observedDays"] and group[-1]["date"] == bar["date"]
                    aggregates = {"open": group[0]["open"], "high": max(r["high"] for r in group),
                                  "low": min(r["low"] for r in group), "close": group[-1]["close"],
                                  "volume": sum(r["volume"] for r in group), "amount": sum(r["amount"] for r in group),
                                  "openInterest": group[-1]["openInterest"]}
                    assert all(near(v, bar[k]) for k, v in aggregates.items())
                assert observed_days == [d["date"] for d in daily]
            category_record["instruments"][instrument["code"]] = {"periods": periods,
                "tradeDate": instrument["tradeDate"], "contract": instrument["activeContract"],
                "changeBasis": instrument["changeBasis"], "sources": instrument["sourcePayloadIds"]}
    return result


def check_etfs(tiles, originals):
    result = {}
    for tile in tiles:
        etf = tile["etf"]
        code = etf["code"]
        ids = etf["sourcePayloadIds"]
        quote_api, (quote,) = originals.read(ids["quote"])
        share_api, (share,) = originals.read(ids["shares"])
        assert quote_api == "fund_daily" and share_api == "fund_share"
        assert quote["ts_code"] == share["ts_code"] == code and day(quote["trade_date"]) == day(share["trade_date"]) == etf["tradeDate"]
        expected = {"marketCap": float(share["fd_share"])*float(quote["close"])/10000,
                    "turnover": float(quote["amount"])/100000, "change": quote["pct_chg"]}
        assert all(near(tile[k], v) for k,v in expected.items())
        assert near(quote["close"], etf["close"]) and near(quote["vol"], etf["volume"]) and near(share["fd_share"], etf["shares"])
        result[code] = {"tradeDate": etf["tradeDate"], "sourceIds": ids, "marketCapYi": tile["marketCap"],
                        "turnoverYi": tile["turnover"], "holdings": len(tile["stocks"]), "stockCount": tile["stockCount"]}
    return result


def membership(session, symbols, as_of):
    config = StockMappingPolicy.load().classification
    data = session.execute(text("""
        with recursive tree as (
            select m.ts_code, c.classification_id, c.name, c.parent_id, c.level
            from core.classification_member m join core.classification c on c.id=m.classification_id
            where m.ts_code=any(:symbols) and m.effective_from<=:as_of
              and (m.effective_to is null or m.effective_to>=:as_of) and c.provider=:provider and c.source=:source
            union
            select t.ts_code, c.classification_id, c.name, c.parent_id, c.level from tree t
            join core.classification c on c.classification_id=t.parent_id
            where c.provider=:provider and c.source=:source
        ) select ts_code,classification_id,level from tree
    """), dict(symbols=symbols, as_of=as_of, provider=config.provider, source=config.source))
    groups = defaultdict(lambda: defaultdict(set))
    for symbol, group, level in data:
        if level in (config.majorLevel, config.subLevel):
            groups["majorIndustry" if level == config.majorLevel else "subIndustry"][group].add(symbol)
    return groups


def check_heatmaps(session, explorer, stocks, as_of):
    expected = membership(session, list(stocks), as_of)
    result = {}
    for category in ("majorIndustry", "subIndustry", "region"):
        tiles = explorer["heatmapData"][category]
        membership_count = Counter()
        if category in expected:
            assert set(expected[category]) == {tile["id"] for tile in tiles}
        for tile in tiles:
            rows = tile["stocks"]
            symbols = {r["symbol"] for r in rows}
            assert len(symbols) == len(rows) == tile["stockCount"]
            if category in expected:
                assert expected[category][tile["id"]] == symbols
            for row in rows:
                source = stocks[row["symbol"]]
                assert source["name"] == row["name"] and (category != "region" or source["area"] == tile["name"])
                for field in ("change", "price", "turnover", "marketCap"):
                    assert near(row[field], source[field]), (category, row["symbol"], field)
            assert near(tile["change"], round(sum(r["change"] for r in rows)/len(rows), 4))
            for field in ("turnover", "marketCap"):
                assert near(tile[field], sum(r[field] for r in rows))
            membership_count.update(symbols)
        assert set(membership_count) == set(stocks)
        result[category] = {"tiles": len(tiles), "uniqueSymbols": len(membership_count),
                            "memberships": sum(membership_count.values())}
    result["concept"] = {"advertised": "concept" in {c["id"] for c in explorer["heatmapCategories"]},
                         "tiles": len(explorer["heatmapData"].get("concept", []))}
    return result


def check_stock_day(session, overview, explorer):
    as_of = overview["marketSnapshot"]["asOf"]
    assert explorer["snapshotLabel"] == as_of
    pool = session.get(PublishedSnapshot, "screener")
    assert str(pool.as_of.date()) == as_of
    codes = [r["symbol"] for r in pool.payload["stocks"]]
    rows = session.execute(text("""
        select s.ts_code, s.name, s.data->>'area' area, p.close price, p.pct_chg change,
            p.amount/100000 turnover, b.total_mv/10000 "marketCap", p.amount, p.high, p.pre_close, p.vol,
            s.list_date, b.data->>'limit_status' limit_status
        from core.security s
        join core.daily_price p on p.ts_code=s.ts_code and p.trade_date=:day
        left join core.daily_basic b on b.ts_code=s.ts_code and b.trade_date=p.trade_date
        where s.ts_code=any(:codes)
    """), {"day": as_of, "codes": codes}).mappings()
    stocks = {r["ts_code"]: r for r in rows}
    assert set(stocks) == set(codes)
    daily = list(stocks.values())
    turnover = sum(r["amount"] for r in daily)
    assert near(overview["turnoverSnapshot"]["value"], turnover)
    assert [r["code"] for r in overview["turnoverTop10"]] == [r["ts_code"] for r in sorted(daily, key=lambda r:-r["amount"])[:10]]
    changes = [r["change"] for r in daily]
    breadth, = overview["breadthSnapshots"]
    assert breadth["rise"] == sum(v>0 for v in changes) and breadth["fall"] == sum(v<0 for v in changes)
    assert breadth["total"] == len(changes) and near(breadth["averageChange"], round(sum(changes)/len(changes), 2))
    for field, descending in (("rankingLeaders", True), ("rankingDecliners", False)):
        ranked = sorted(daily, key=lambda r:r["change"], reverse=descending)[:10]
        for actual, source in zip(explorer[field], ranked, strict=True):
            assert actual["symbol"] == source["ts_code"] and near(actual["price"], source["price"]) and near(actual["change"], source["change"])
    for top in overview["turnoverTop10"]:
        row = stocks[top["code"]]
        assert near(top["turnover"], row["amount"]) and near(top["change"], row["change"])
    sentiments = overview["sentimentSnapshot"]
    threshold = Decimal(sentiments["policy"]["threshold"])
    eligible = [r for r in daily if r["vol"] and r["pre_close"] and r["list_date"] != date.fromisoformat(as_of)]
    hit = sum(r["high"]>=r["pre_close"]*(1+threshold) for r in eligible)
    closed = sum(r["price"]>=r["pre_close"]*(1+threshold) for r in eligible)
    independent_counts = dict(total=len(eligible), rise=sum(r["price"]>r["pre_close"] for r in eligible),
        thresholdRise=closed, thresholdFall=sum(r["price"]<=r["pre_close"]*(1-threshold) for r in eligible),
        touched=hit, retreat=hit-closed)
    assert independent_counts == sentiments["counts"]
    return {"businessDay": as_of, "samples": len(daily), "poolVersion": pool.version,
            "turnoverQian": str(turnover), "breadth": breadth, "limitStats": overview["limitStats"],
            "coreLimitStatusNonNull": sum(r["limit_status"] is not None for r in daily),
            "sentiment": {"counts": independent_counts, "status": sentiments["status"], "score": sentiments["score"]},
            "heatmaps": check_heatmaps(session, explorer, stocks, as_of)}


def inspect(factory, session):
    app = FastAPI()
    app.include_router(create_router(provider=MarketDatabaseProvider(SnapshotRepository(factory))))
    summaries, products = {}, {}
    with TestClient(app) as http:
        for name in ("market-overview", "market-explorer"):
            response = http.get(f"/api/data/{name}")
            response.raise_for_status()
            pointer = session.get(PublishedSnapshot, name)
            data = response.json()
            assert data["data"] == pointer.payload and data["meta"]["version"] == pointer.version
            history = session.execute(text("""
                select as_of::date as day, count(*) from mart.page_snapshot where product=:product group by 1 order by 1
            """), {"product": name}).all()
            same = session.scalar(text("""
                select count(*) from mart.page_snapshot where product=:product and snapshot_version=:version
                and payload::jsonb=(select payload::jsonb from mart.published_snapshot where product=:product)
            """), {"product": name, "version": pointer.version})
            assert same == 1
            summaries[name] = {"meta": data["meta"], "rootKeys": sorted(data["data"]),
                               "historyByDay": {str(d): n for d,n in history},
                               "missingData": data["data"].get("missingData", [])}
            products[name] = data["data"]
    overview, explorer = products["market-overview"], products["market-explorer"]
    originals = SourceRows(session)
    quotes = {}
    for field in ("globalIndices", "etfQuotes"):
        quotes[field] = {}
        for q in overview[field]:
            raw = session.execute(text("""
                select close,pct_chg,api_name,source_payload_id,captured_at from core.market_daily_quote
                where asset_code=:code and trade_date=:day and asset_type=:kind and provider='tushare'
            """), {"code": q["code"], "day": q["tradeDate"], "kind": "indices" if field=="globalIndices" else "etfs"}).mappings().one()
            assert near(q["change"], raw["pct_chg"]) and q["source"] == "tushare:"+raw["api_name"]
            if field == "globalIndices":
                assert q["value"] == f"{raw['close']:,.2f}"
            api_name, original_rows = originals.read(raw["source_payload_id"])
            original, = [r for r in original_rows if r["ts_code"] == q["code"] and day(r["trade_date"]) == q["tradeDate"]]
            assert api_name == raw["api_name"] and near(original["close"], raw["close"]) and near(original["pct_chg"], q["change"])
            assert raw["captured_at"].isoformat() == q["capturedAt"]
            quotes[field][q["code"]] = {"tradeDate": q["tradeDate"], "source": q["source"],
                    "sourcePayloadId": raw["source_payload_id"], "capturedAt": q["capturedAt"],
                    "intradayPoints": len(q.get("intraday") or []), "calendarStatus": q["calendarStatus"]}
    news = explorer["marketNews"]
    for item in news:
        row = session.get(ContentDocument, int(item["id"]))
        assert row and row.title==item["title"] and row.source_url==item["sourceUrl"] and row.source==item["source"]
        assert row.published_at.isoformat()==item["publishedAt"] and row.copyright_status==item["copyrightStatus"]
    return {"products": summaries, "stockDay": check_stock_day(session, overview, explorer), "quotes": quotes,
            "marketStatuses": overview["marketStatuses"], "fundFlow": overview["fundFlowSnapshot"],
            "commodityMetadata": explorer["commodityMetadata"], "commodities": check_commodities(explorer, originals),
            "etfTiles": check_etfs(explorer["heatmapData"]["etf"], originals), "regionMetadata": explorer["regionMetadata"],
            "verifiedRawPagesByApi": Counter(api for api, rows in originals.cache.values()),
            "news": {"count": len(news), "publishedFrom": min((r["publishedAt"] for r in news), default=None),
                     "publishedThrough": max((r["publishedAt"] for r in news), default=None),
                     "sources": Counter(r["source"] for r in news), "copyright": Counter(r["copyrightStatus"] for r in news)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        evidence = inspect(factory, session)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
