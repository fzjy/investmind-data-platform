"""在同一个只读事务复核市场历史版中的成交、宽度、排名、行业及商品。"""
import argparse
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import runpy
from zoneinfo import ZoneInfo


def signature(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def compare_overview(body, stocks, near):
    breadth, = body["breadthSnapshots"]
    turnover = body["turnoverSnapshot"]
    daily = list(stocks.values())
    changes = [r["change"] for r in daily]
    rise, fall = sum(c > 0 for c in changes), sum(c < 0 for c in changes)
    equal = len(changes) - rise - fall
    amount = sum(r["amount"] for r in daily)
    assert near(amount, turnover.get("value", turnover.get("total")))
    assert rise == breadth.get("rise", breadth.get("advance"))
    assert fall == breadth.get("fall", breadth.get("decline"))
    if "total" in breadth:
        assert breadth["total"] == len(changes)
        assert near(round(sum(changes)/len(changes), 2), breadth["averageChange"])
    else:
        assert equal == breadth["flat"]
    top = body["turnoverTop10"]
    assert len(top) == min(len(stocks), 10)
    for item, expected in zip(top, sorted(daily, key=lambda r: -r["amount"])):
        assert item.get("code", item.get("symbol")) == expected["symbol"]
        assert near(item.get("turnover", item.get("amount")), expected["amount"])
        if "change" in item:
            assert near(item["change"], expected["change"])
    return {"rise": rise, "fall": fall, "flat": equal, "turnoverQianYuan": str(amount),
            "topRows": len(top)}


def compare_ranking(body, stocks, near):
    counts = {}
    for name, descending in (("rankingLeaders", True), ("rankingDecliners", False)):
        actual = body[name]
        expected = sorted((r["change"] for r in stocks.values()), reverse=descending)[:10]
        assert len({r["symbol"] for r in actual}) == len(expected) == len(actual)
        for item, change in zip(actual, expected):
            original = stocks[item["symbol"]]
            assert near(change, item["change"]) and near(original["change"], change)
            assert near(original["price"], item["price"])
        counts[name] = len(expected)
    return counts


def compare_tiles(heat, stocks, catalog, near):
    verified = {}
    for category in ("majorIndustry", "subIndustry", "region"):
        tiles = heat.get(category, [])
        if not tiles:
            verified[category] = {"tiles": 0}
            continue
        group_symbols = {t["id"]: {r["symbol"] for r in t["stocks"]} for t in tiles}
        assert len(group_symbols) == len(tiles)
        assert set.union(*group_symbols.values()) == set(stocks)
        if category != "region":
            assert group_symbols == dict(catalog[category])
        total = 0
        for tile in tiles:
            rows = tile["stocks"]
            assert tile["stockCount"] == len(rows) == len(group_symbols[tile["id"]])
            total += len(rows)
            for r in rows:
                core = stocks[r["symbol"]]
                for key in ("price", "change", "turnover", "marketCap"):
                    assert near(r[key], core[key]), (category, r["symbol"], key)
                if category == "region":
                    assert tile["name"] == core["area"]
            for key in ("turnover", "marketCap"):
                assert near(tile[key], sum(r[key] for r in rows if r[key] is not None))
            assert near(tile["change"], round(sum(r["change"] for r in rows) / len(rows), 4))
        verified[category] = {"tiles": len(tiles), "memberships": total}
    return verified


def verify_price_sources(session, shared, expected):
    from sqlalchemy import text
    pages = session.execute(text("""select p.id,p.api_name,p.captured_at,p.content_hash,p.payload
        from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id
        where b.source='tushare' and p.api_name in ('daily','daily_basic') and b.dataset=p.api_name
        order by p.id""")).mappings()
    days = {d.strftime("%Y%m%d"): d for d, _ in expected}
    matches, alternatives = defaultdict(list), Counter()
    hashes = set()
    for page in pages:
        body = page["payload"]
        columns = {name:i for i,name in enumerate(body["fields"])}
        api = page["api_name"]
        fields = ("close", "pct_chg", "amount") if api == "daily" else ("total_mv",)
        if not set(fields).issubset(columns):
            continue
        for row in body["items"]:
            day = days.get(row[columns["trade_date"]])
            key = (day, row[columns["ts_code"]])
            if key not in expected:
                continue
            if page["id"] not in hashes:
                assert shared["content_hash"](body) == page["content_hash"]
                hashes.add(page["id"])
            source = expected[key]
            values = (source["price"], source["change"], source["amount"]) if api == "daily" else (source["marketCap"]*10000,)
            if all(shared["near"](row[columns[field]], value) for field,value in zip(fields,values)):
                matches[(*key, api)].append((page["id"], page["captured_at"].astimezone(ZoneInfo("Asia/Shanghai")).date()))
            else:
                alternatives[(*key, api)] += 1
    result = {}
    for day in sorted(days.values()):
        securities = sorted(code for d,code in expected if d == day)
        result[str(day)] = {}
        for api in ("daily", "daily_basic"):
            covered = {s:matches[(day,s,api)] for s in securities if matches[(day,s,api)]}
            result[str(day)][api] = {
                "requiredSymbols":len(securities), "matchedSymbols":len(covered),
                "withoutMatch": sorted(set(securities)-set(covered)),
                "additionalDistinctValues": [s for s in securities if alternatives[(day,s,api)]],
                "earliestMatchingPagesBySymbol":{s:min(pages,key=lambda r:(r[1],r[0]))[0] for s,pages in covered.items()},
                "firstCaptureRange": [str(bound(min(cap for _,cap in pages) for pages in covered.values())) for bound in (min,max)] if covered else [],
            }
    return result


def audit(root):
    from sqlalchemy import text
    shared = runpy.run_path(str(root / "scripts/audit-market-supply.py"))
    profiles, versions, artifacts = {}, {}, {}
    pool_candidates = defaultdict(dict)
    source_by_date, membership_cache, expected_prices = {}, {}, {}
    near = shared["near"]
    with shared["open_readonly_factory"](shared["settings"].database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        assert session.scalar(text("show transaction_isolation")) == "repeatable read"
        originals = shared["SourceRows"](session)
        for day, ver, codes in session.execute(text("""select as_of,snapshot_version,array_agg(ts_code order by ts_code)
                from mart.stock_screener_daily group by 1,2""")):
            pool_candidates[day][ver] = tuple(codes)
        snapshot_rows = session.execute(text("""select as_of::date,snapshot_version,product,
            payload::jsonb - 'marketNews' - 'commodityData' - 'heatmapData' as body,
            payload::jsonb -> 'heatmapData' as heat, payload::jsonb -> 'commodityData' as commodities
            from mart.page_snapshot where product in ('market-overview','market-explorer') order by as_of,id"""))
        for page_day, ver, product, body, heat, commodities in snapshot_rows:
            overview = product == "market-overview"
            market = body.get("marketSnapshot", {})
            field_day = date.fromisoformat(market.get("asOf", market.get("date")) if overview else body["snapshotLabel"])
            candidates = pool_candidates[page_day]
            if overview:
                breadth, = body["breadthSnapshots"]
                count = breadth["total"] if "total" in breadth else sum(breadth[k] for k in ("advance", "decline", "flat"))
                ranked = body["turnoverTop10"]
            else:
                ranked = body["rankingLeaders"]
                count = None
            complete_rank = count is not None and len(ranked) == count or not overview and len(ranked) < 10
            if complete_rank:
                codes = tuple(sorted(r.get("symbol",r.get("code")) for r in ranked))
                assert len(set(codes)) == len(codes)
                if not overview:
                    assert set(codes) == {r["symbol"] for r in body["rankingDecliners"]}
                matching = [key for key, pool in candidates.items() if pool == codes]
                assert matching
                basis = "complete-snapshot-ranking-and-matching-same-page-day-pool"
            else:
                unique = set(candidates.values())
                assert len(unique) == 1, (page_day, "ambiguous-pool")
                codes, = unique
                if count is not None:
                    assert len(codes) == count
                basis = "same-page-day-screener-unanimous-members"
            if field_day not in source_by_date:
                rows = session.execute(text("""
                    select p.ts_code symbol, p.close price, p.pct_chg change, p.amount,
                        p.amount/100000 turnover, b.total_mv/10000 "marketCap", s.data->>'area' area
                    from core.daily_price p join core.security s on s.ts_code=p.ts_code
                    left join core.daily_basic b on b.ts_code=p.ts_code and b.trade_date=p.trade_date
                    where p.trade_date=:day"""), {"day":field_day}).mappings()
                source_by_date[field_day] = {r["symbol"]: r for r in rows}
            stocks = {code: source_by_date[field_day][code] for code in codes}
            expected_prices.update({(field_day, code): row for code,row in stocks.items()})
            block = {"fieldDay": str(field_day), "symbols": len(codes), "symbolSetHash": signature(codes),
                     "coreDayRows": len(source_by_date[field_day]), "scopeBasis": basis}
            if overview:
                block["breadthAndTurnover"] = compare_overview(body, stocks, near)
            else:
                block["ranks"] = compare_ranking(body, stocks, near)
                key = (field_day, codes)
                if key not in membership_cache:
                    membership_cache[key] = shared["membership"](session, list(codes), str(field_day))
                heat = heat or {}
                block["heatmaps"] = compare_tiles(heat, stocks, membership_cache[key], near)
                for family, data in (("etf", heat.get("etf") or []), ("commodity", commodities or {})):
                    if not data:
                        block[family] = None
                        continue
                    digest = signature(data)
                    if digest not in artifacts:
                        if family == "etf":
                            verified = shared["check_etfs"](data, originals)
                            artifacts[digest] = {"family":family,"verifiedEtfs":len(verified)}
                        else:
                            verified = shared["check_commodities"]({"commodityData":data}, originals)
                            instruments = [v for cat in verified.values() for v in cat["instruments"].values()]
                            artifacts[digest] = {"family":family,"instruments":len(instruments),
                                "bars": {period: sum(i["periods"][period]["bars"] for i in instruments)
                                         for period in ("daily", "weekly", "monthly")}}
                    block[family] = digest
            key = signature(block)[:16]
            if key in profiles:
                assert block == profiles[key]
            profiles[key] = block
            versions.setdefault(product, {}).setdefault(str(page_day), {}).setdefault(key, []).append(ver)
        db_count = dict(session.execute(text("""select product,count(*) from mart.page_snapshot
                    where product in ('market-overview','market-explorer') group by product""")).all())
        assert db_count == {p:sum(len(v) for groups in dates.values() for v in groups.values()) for p,dates in versions.items()}
        evidence = {"checkedAt": session.scalar(text("select now()")).isoformat(), "counts":db_count, "versions":versions,
                    "profiles":profiles, "rawVerifiedArtifacts":artifacts, "rawPagesByApi":dict(Counter(api for api,_ in originals.cache.values())),
                    "historicalPoolSizes":{str(d):{v:len(c) for v,c in sorted(pools.items())} for d,pools in sorted(pool_candidates.items())},
                    "stockOriginals":verify_price_sources(session,shared,expected_prices)}
    return evidence


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = audit(args.root.resolve())
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(result["counts"], result["rawPagesByApi"])


if __name__ == "__main__":
    main()
