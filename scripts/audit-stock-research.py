"""按已发布选股池独立检验研究接口的比较、分布统计与同日价格比。"""

import json
from math import isclose
from pathlib import Path
from statistics import mean, stdev
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.research_tools import ResearchRequest, ResearchToolsService


def value(row, key):
    if row is None:
        return None
    source = row if key in ("pe", "pb", "marketCap") else row.get("researchValues", {})
    number = source.get(key)
    if type(number) not in (int, float) or (key in ("pe", "pb", "marketCap") and number <= 0):
        return None
    return number


def equal(actual, expected):
    if expected is None:
        return actual is None
    return actual is not None and isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)


def audit_query(service, repo, stocks, symbol, peer_symbol=None):
    result = service.query(ResearchRequest(symbol=symbol, peerSymbol=peer_symbol))
    data = result.data
    assert data["scope"]["universeCount"] == len(stocks) and len(data["metrics"]) == 8
    current = stocks[symbol]
    peer = stocks[data["peer"]["symbol"]] if data["peer"] else None
    assert peer_symbol is None or peer["symbol"] == peer_symbol
    assert {row["currency"] for row in data["instruments"]} == {"CNY"}
    if peer_symbol is None:
        candidates = [r for r in stocks.values() if r["symbol"] != symbol and current["majorIndustry"]
                      and r["majorIndustry"] == current["majorIndustry"] and value(r, "marketCap") is not None]
        ordered = sorted(candidates, key=lambda r: (-r["marketCap"], r["symbol"]))
        assert peer == (ordered[0] if ordered else None)
    metrics = {}
    for metric in data["metrics"]:
        key = metric["key"]
        own = value(current, key)
        other = value(peer, key)
        # 当前开发池全为人民币 A 股，金额分布以此为前提。
        values = [n for row in stocks.values() if (n := value(row, key)) is not None]
        local = [n for row in stocks.values() if row["majorIndustry"] == current["majorIndustry"]
                 and (n := value(row, key)) is not None]
        def percentile(series):
            return 100 * sum(n <= own for n in series) / len(series) if own is not None and len(series) >= 2 else None
        deviation = stdev(values) if len(values) >= 2 else 0
        expected = {"value": own, "peerValue": other, "difference": own - other if None not in (own, other) else None,
                    "sampleCount": len(values), "industrySampleCount": len(local), "percentile": percentile(values),
                    "industryPercentile": percentile(local),
                    "zScore": (own - mean(values))/deviation if deviation and own is not None else None}
        for field, number in expected.items():
            assert equal(metric[field], number), (symbol, key, field, metric[field], number)
        metrics[key] = {"label": metric["label"], **expected}
    pair = data["pair"]
    if peer:
        legs = [repo.read("stock-workbench:" + row["symbol"]) for row in (current, peer)]
        if all((leg.meta.source, leg.meta.version, leg.meta.as_of) ==
               (result.meta.source, result.meta.version, result.meta.as_of) for leg in legs):
            cutoff = result.meta.as_of[:10]
            closes = [{p["date"]: p["close"] for p in leg.raw["dailyChart"]["candles"] if p["date"] <= cutoff}
                      for leg in legs]
            days = sorted(closes[0].keys() & closes[1].keys())
            assert pair["status"] == "available" and [p["date"] for p in pair["points"]] == days
            assert all(equal(p["value"], closes[0][p["date"]] / closes[1][p["date"]]) for p in pair["points"])
            assert pair["asOf"] == days[-1] and equal(pair["ratio"], pair["points"][-1]["value"])
        else:
            assert pair["status"] == "unavailable" and not pair["points"] and pair["ratio"] is None
    else:
        assert pair["status"] == "unavailable" and not pair["points"]
    return {"symbol": symbol, "peer": peer["symbol"] if peer else None, "snapshot": result.meta.version,
            "metrics": metrics, "pair": {k:v for k,v in pair.items() if k != "points"},
            "firstDay": pair["points"][0]["date"] if pair["points"] else None, "comparedDays": len(pair["points"])}


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        repo = SnapshotRepository(factory)
        stocks = {r["symbol"]: r for r in repo.read("screener").raw["stocks"]}
        service = ResearchToolsService(StocksDatabaseProvider(repo, factory))
        cases = [("600519.SH", "000858.SZ"), ("600519.SH", "002594.SZ"), ("000001.SZ", None)]
        report = [audit_query(service, repo, stocks, s, p) for s,p in cases]
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
