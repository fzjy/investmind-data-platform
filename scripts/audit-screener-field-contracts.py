"""复核真实快照下完整选股 HTTP 契约，逐项比对单条件成员及简单排序。"""

import hashlib
import json
import operator
from argparse import ArgumentParser
from copy import deepcopy
from math import isclose, isfinite
from pathlib import Path
from statistics import mean, stdev
import sys
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.bank_scoring import rules
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.models import BoardId, ExclusionKey
from app.stocks.rea_fields import REA_PRESETS, REA_SORT_FIELDS, REA_THRESHOLD_UI
from app.stocks.routes import create_router

OPS = {"gt": operator.gt, "lt": operator.lt, "gte": operator.ge, "lte": operator.le}

# 与 InvestMind 发布时的七个排序模板对应，保留独立计算所用权重和方向。
ADVANCED_SORTS = {
    "高股息": {"dividendYield": 70, "expectedDividendYieldNTM": 30},
    "分红稳健": {"dividendYield": 40, "dividendYears": 35, "payoutRatio": 25},
    "质量优先": {"roe": 40, "roic": 30, "realMoneyIndex": 30},
    "便宜好公司": {"pePercentile": -40, "pbPercentile": -30, "roe": 30},
    "真钱＋分红": {"realMoneyIndexCum5y": 40, "dividendYield": 30, "expectedDividendYieldNTM": 30},
    "增长偏好": {"revenueGrowingFy": 60, "ocfGrowingFy": 40},
    "大盘稳健": {"marketCap": 40, "dividendYield": 30, "roe": 30},
}


def raw_number(row, key):
    research = row.get("researchValues", {})
    number = research[key] if key in research else row.get(key)
    if type(number) not in (float, int) or not isfinite(number):
        return None
    return number


def value(row, key):
    number = raw_number(row, key)
    if number is not None and key in ("pe", "pb") and number <= 0:
        return None
    return number


def symbols(rows):
    return [r["symbol"] for r in rows]


def digest(rows):
    return hashlib.sha256("\n".join(symbols(rows)).encode()).hexdigest()


def advanced_order(stocks, weights):
    scores = {row["symbol"]: [] for row in stocks}
    for field, weight in weights.items():
        series = {row["symbol"]: value(row, field) for row in stocks}
        finite = [v for v in series.values() if v is not None]
        center = mean(finite) if finite else 0
        spread = stdev(finite) if len(finite) > 1 else 0
        zscores = [(v-center)/spread for v in finite] if spread else []
        worst = max([0, *zscores]) + 1 if weight < 0 else min([0, *zscores]) - 1
        for symbol, v in series.items():
            if v is not None:
                scores[symbol].append(weight * (v - center) / spread if spread else 0)
        if field in ("pe", "pb"):
            for row in stocks:
                original = raw_number(row, field)
                if original is not None and original <= 0:
                    scores[row["symbol"]].append(weight * worst)
    adjusted = {symbol: sum(terms) * len(terms) / len(weights) if terms else float("-inf")
                for symbol, terms in scores.items()}
    return sorted(adjusted, key=lambda symbol: (-adjusted[symbol], symbol))


def core_comparison(session, stocks, as_of):
    rows = session.execute(text("""
        select s.ts_code, s.name, p.close as price, p.pct_chg as change,
               b.pe, b.pb, b.total_mv / 10000 as "marketCap"
        from core.security s
        left join core.daily_price p on p.ts_code = s.ts_code and p.trade_date = :day
        left join core.daily_basic b on b.ts_code = s.ts_code and b.trade_date = :day
        where s.ts_code = any(:symbols)
    """), {"day": as_of[:10], "symbols": symbols(stocks)}).mappings()
    core = {r["ts_code"]: r for r in rows}
    assert set(core) == set(symbols(stocks))
    report = {}
    for field in ("name", "price", "change", "pe", "pb", "marketCap"):
        mismatches = []
        both_null = 0
        for row in stocks:
            published = row[field]
            original = core[row["symbol"]][field]
            if published is None and original is None:
                both_null += 1
                continue
            if field == "name":
                same = published == original
            else:
                same = published is not None and original is not None and isclose(
                    published, float(original), rel_tol=1e-9, abs_tol=1e-8)
            if not same:
                mismatches.append({"symbol": row["symbol"], "api": published,
                                   "core": str(original) if original is not None else None})
        report[field] = {"equalNonNull": len(stocks) - both_null - len(mismatches),
                         "equalNull": both_null, "different": mismatches}
        assert not mismatches, (field, mismatches)
    return report


def excluded_by(row):
    trend = [v for v in row.get("profitTrend") or [] if v is not None]
    payouts = row.get("payoutRatios") or []
    pe, money = row.get("pe"), row.get("realMoneyIndex")
    revenue, cashflow = row.get("revenueGrowth"), row.get("cashflowGrowth")
    return {
        "loss": row.get("isLoss") is True or (pe is not None and pe < 0),
        "st": row.get("isST") is True or "ST" in row["name"],
        "inventoryRisk": row.get("inventoryRisk") is True,
        "lowRealMoney": money is not None and money < 0.8,
        "netDebt": row.get("netDebt") is True,
        "performanceDecline": len(trend) >= 2 and all(b < a for a, b in zip(trend, trend[1:])),
        "noDividend": len(payouts) == 3 and all(v == 0 for v in payouts),
        "revenueDecline": revenue is not None and revenue <= 0,
        "cashflowDecline": cashflow is not None and cashflow <= 0,
    }


def inspect(client, session):
    request = {"filters": {"preset": None, "thresholds": {}, "enabledThresholds": [], "exclusions": [],
                           "boards": list(get_args(BoardId)), "categories": {
                               "majorIndustry": [], "subIndustry": [], "region": [], "concept": []}},
               "sortRules": [], "sortMode": "quick", "page": 1, "pageSize": 200}

    reference_snapshot = None

    def call(filter_changes=None, **changes):
        body = deepcopy(request)
        body["filters"].update(filter_changes or {})
        body.update(changes)
        items = []
        first = None
        while True:
            response = client.post("/api/v1/stocks/screener", json=body)
            response.raise_for_status()
            data = response.json()["data"]
            if first is None:
                first = data
            assert data["snapshot"] == first["snapshot"] and data["total"] == first["total"]
            if reference_snapshot is not None:
                assert data["snapshot"] == reference_snapshot, "审计期间发布版本发生变化，请重新执行"
            assert (data["page"], data["pageSize"]) == (body["page"], body["pageSize"])
            expected = min(data["pageSize"], data["total"] - len(items))
            assert len(data["items"]) == expected, (body["page"], expected)
            items.extend(data["items"])
            if len(items) == data["total"]:
                break
            body["page"] += 1
        assert len(set(symbols(items))) == len(items), "分页结果有重复证券"
        return first | {"items": items}

    baseline = call()
    stocks = baseline["items"]
    assert stocks, "当前开发池为空，无法验证选股成员及顺序"
    reference_snapshot = baseline["snapshot"]
    counts = baseline["bootstrap"]["availableCounts"]
    result = dict(snapshot=baseline["snapshot"], rows=len(stocks))
    probe_size = 10
    paged = call(pageSize=probe_size)["items"]
    assert symbols(paged) == symbols(stocks), "分页改变了成员或顺序"
    result["pageCoverage"] = dict(pageSize=probe_size, pages=(len(paged)+probe_size-1)//probe_size,
                                  uniqueSymbols=len(paged), orderedSymbolsHash=digest(paged))
    result["sameDayCore"] = core_comparison(session, stocks, baseline["snapshot"]["asOf"])
    roic = session.execute(text("""
        select ts_code, unit, value from derived.metric_value
        where metric_id = 'roic' and formula_version = '1.0.0'
          and as_of = :day and ts_code = any(:symbols)
    """), {"day": baseline["snapshot"]["asOf"][:10], "symbols": symbols(stocks)}).mappings()
    by_symbol = {r["ts_code"]: r for r in roic}
    amounts = [r["roic"] for r in stocks if r["roic"] is not None]
    result["roicUnits"] = {
        "storedRows": len(by_symbol), "storedNonNullUnits": sorted({r["unit"] for r in by_symbol.values()
                                                                  if r["value"] is not None and r["unit"]}),
        "publishedMinMax": [min(amounts), max(amounts)] if amounts else [],
        "publishedEqualsStoredNonNull": sum(
            r["roic"] is not None and r["symbol"] in by_symbol and by_symbol[r["symbol"]]["value"] is not None
            and isclose(r["roic"], float(by_symbol[r["symbol"]]["value"]), abs_tol=1e-8) for r in stocks),
    }
    numeric = {}
    for item in REA_THRESHOLD_UI:
        key, direction, target = item["key"], item["direction"], item["defaultValue"]
        measured = call({"thresholds": {key: target}, "enabledThresholds": [key]})
        expected = [r for r in stocks if value(r, key) is not None and OPS[direction](value(r, key), target)]
        available = sum(value(r, key) is not None for r in stocks)
        assert available == counts[key] and symbols(expected) == symbols(measured["items"]), key
        missing = [r["symbol"] for r in stocks if value(r, key) is None]
        numeric[key] = dict(unit=item["unit"], operator=direction, threshold=target,
                            available=available, matched=len(expected), missingSymbols=missing,
                            missingWithReason=sum(bool(r.get("listMissingReasons", {}).get(key))
                                                  for r in stocks if r["symbol"] in missing))
    result["thresholds"] = numeric
    result["presets"] = {}
    for preset in REA_PRESETS:
        patch = preset["patch"]
        limits = patch.get("thresholds", {})
        directions = patch.get("thresholdDirections", {})
        filtered = call({"preset": preset["id"], "thresholds": limits, "enabledThresholds": list(limits),
                         "thresholdDirections": directions})["items"]
        expected = [r for r in stocks if all(value(r, k) is not None and
                    OPS[directions.get(k, numeric[k]["operator"])](value(r, k), v) for k, v in limits.items())]
        assert symbols(filtered) == symbols(expected)
        result["presets"][preset["id"]] = dict(label=preset["label"], thresholds=limits,
                                                directionOverrides=directions, matched=len(filtered))
    sorts = {}
    for key in dict.fromkeys([s["value"] for s in REA_SORT_FIELDS] + ["profitGrowth","revenueGrowingFy","ocfGrowingFy"]):
        rule = {"field":key, "direction":"desc", "enabled":True}
        measured = call(sortRules=[rule])["items"]
        expected = sorted(stocks, key=lambda r: (value(r, key) is None, -(value(r,key) or 0), r["symbol"]))
        assert symbols(measured) == symbols(expected), key
        sorts[key] = {"usable":sum(value(r,key) is not None for r in stocks), "orderedSymbolsHash":digest(measured)}
    result["quickSort"] = sorts
    result["advancedSort"] = {}
    for template, weights in ADVANCED_SORTS.items():
        ruleset = [{"field": k, "direction": "desc" if w > 0 else "asc",
                    "enabled": True, "weight": abs(w)} for k, w in weights.items()]
        ordered = call(sortMode="advanced", sortRules=ruleset)["items"]
        assert symbols(ordered) == advanced_order(stocks, weights), template
        result["advancedSort"][template] = {"rules": ruleset, "orderedSymbolsHash": digest(ordered)}
    result["exclusionPassing"], result["boardCounts"] = {}, {}
    result["exclusionMembers"], result["boardMembers"] = {}, {}
    for key in get_args(ExclusionKey):
        rows = call({"exclusions": [key]})["items"]
        expected = [r for r in stocks if not excluded_by(r)[key]]
        assert symbols(rows) == symbols(expected), key
        result["exclusionPassing"][key] = len(rows)
        result["exclusionMembers"][key] = digest(rows)
    for key in get_args(BoardId):
        rows = call({"boards": [key]})["items"]
        assert symbols(rows) == symbols([r for r in stocks if r["board"] == key]), key
        result["boardCounts"][key] = len(rows)
        result["boardMembers"][key] = digest(rows)
    result["categorySamples"] = {}
    for category in request["filters"]["categories"]:
        def labels(row):
            return row["concepts"] if category == "concept" else [row[category]]
        sample = next((item for row in stocks for item in labels(row) if item), None)
        if sample is None:
            result["categorySamples"][category] = None
            continue
        selections = {**request["filters"]["categories"], category: [sample]}
        filtered = call({"categories": selections})["items"]
        assert symbols(filtered) == symbols([row for row in stocks if sample in labels(row)])
        result["categorySamples"][category] = {"value": sample, "matched": len(filtered)}
    result["displayFieldCoverage"] = {}
    for field in sorted(set().union(*(r.keys() for r in stocks))):
        cells = [r.get(field) for r in stocks]
        result["displayFieldCoverage"][field] = dict(present=sum(field in r for r in stocks),
                                                     nonNull=sum(v is not None for v in cells))
    bank_cases = {}
    bank_rules = rules()
    for key, preset in bank_rules["presets"].items():
        cfg = {"divisor":preset["divisor"],"weights":preset["weights"],"sort":"score-desc"}
        data = call(bank=cfg)
        rows = data["items"]
        assert set(symbols(rows)) == {r["symbol"] for r in stocks if r.get("majorIndustry") == "银行"}
        details = [r["bankEvaluation"] for r in rows]
        coverage = {f["field"]:sum(e["fields"][f["field"]] is not None for e in details) for f in bank_rules["fields"]}
        assert coverage == data["bank"]["availableCounts"]
        bank_cases[key] = dict(algorithmVersion=data["bank"]["algorithmVersion"], symbols=symbols(rows),
                               nonNullScores=sum(e["total"] is not None for e in details), availableFields=coverage)
    bank_fields = {}
    all_banks = call(bank={})["items"]
    for field in bank_rules["fields"]:
        key=field["field"]
        cfg={"conditions":[{"field":key,"operator":"gte","value":0}]}
        rows=call(bank=cfg)["items"]
        expect=[r for r in all_banks if r["bankEvaluation"]["fields"][key] is not None
                and r["bankEvaluation"]["fields"][key]>=0]
        assert symbols(rows)==symbols(expect)
        bank_fields[key]=len(rows)
    result["bankPresets"]=bank_cases
    result["bankFieldsAtLeastZero"]=bank_fields
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="全部验证通过后保存证据 JSON")
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as s:
        assert s.scalar(text("show transaction_read_only")) == "on"
        api = FastAPI()
        api.include_router(create_router(provider=StocksDatabaseProvider(SnapshotRepository(factory),factory)))
        with TestClient(api) as client:
            report=inspect(client, s)
        payload = json.dumps(report, ensure_ascii=True, indent=2)
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)


if __name__ == "__main__":
    main()
