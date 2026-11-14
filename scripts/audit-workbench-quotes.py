"""逐证券对照已发布工作台、Core 日行情与复权因子，只读同一数据库快照。"""

from bisect import bisect_right
from calendar import monthrange
from collections import Counter
import json
from math import isclose
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.service import WorkbenchService


def matches(actual, expected):
    if expected is None or isinstance(expected, str):
        return actual == expected
    return isinstance(actual, (int, float)) and isclose(actual, float(expected), rel_tol=1e-8, abs_tol=1e-8)


def inspect_symbol(session, service, symbol, cutoff, version):
    product = service.get_workbench(symbol)
    assert product.meta.version == version and product.meta.as_of[:10] == cutoff.isoformat()
    data = product.data
    assert data["symbol"] == symbol and data["quote"]["symbol"] == symbol
    year = cutoff.year - 3
    start = cutoff.replace(year=year, day=min(cutoff.day, monthrange(year, cutoff.month)[1]))
    values = {"code": symbol, "day": cutoff, "start": start}
    earlier = session.scalar(text("""
        select count(*) from core.daily_price where ts_code=:code and trade_date < :start
    """), values)
    name = session.execute(text("select name from core.security where ts_code=:code"), values).scalar_one()
    daily = session.execute(text("""
        select trade_date, open, high, low, close, pct_chg, pre_close, vol
        from core.daily_price where ts_code=:code and trade_date between :start and :day order by trade_date
    """), values).mappings().all()
    basic = session.execute(text("""
        select trade_date, pe, pb, data->>'pe_ttm' as pe_ttm, total_mv/10000 as cap
        from core.daily_basic where ts_code=:code and trade_date between :start and :day order by trade_date desc limit 1
    """), values).mappings().one()
    factor_rows = session.execute(text("""
        select trade_date, adj_factor from core.adjustment_factor
        where ts_code=:code and trade_date between :start and :day and adj_factor is not null order by trade_date
    """), values).all()
    dates = [day for day, _ in factor_rows]
    factors = [factor for _, factor in factor_rows]
    latest = daily[-1]
    previous = latest["pre_close"]
    if previous is None:
        change = latest["pct_chg"]
        previous = latest["close"] if change in (None, -100) else latest["close"] / (1 + change / 100)
    quote = {"name": name, "price": latest["close"], "previousClose": previous, "changePercent": latest["pct_chg"],
             "marketCap": basic["cap"], "pe": basic["pe"], "pb": basic["pb"],
             "peTtm": float(basic["pe_ttm"]) if basic["pe_ttm"] is not None else None}
    errors = Counter()
    for key, expected in quote.items():
        if not matches(data["quote"].get(key), expected):
            errors["quote." + key] += 1
    candles = data["dailyChart"]["candles"]
    core = {row["trade_date"].isoformat(): row for row in daily}
    published_days = [point["date"] for point in candles]
    if not published_days or published_days != sorted(set(published_days)) or published_days[-1] != latest["trade_date"].isoformat():
        errors["candles.dates"] += 1
    if set(published_days) != set(core):
        errors["candles.windowDates"] += len(set(published_days) ^ set(core))
    for point in candles:
        row = core.get(point["date"])
        if row is None:
            errors["candles.missingSourceDate"] += 1
            continue
        index = bisect_right(dates, row["trade_date"]) - 1
        ratio = factors[index] / factors[-1] if index >= 0 and factors[-1] else 1
        for key in ("open", "high", "low", "close", "volume"):
            source = row["vol"] if key == "volume" else row[key]
            expected = source if source is None or key == "volume" else source * ratio
            if not matches(point.get(key), expected):
                errors["candles." + key] += 1
    return {"version": version, "asOf": cutoff.isoformat(), "priceDate": latest["trade_date"].isoformat(),
            "basicDate": basic["trade_date"].isoformat(), "quoteFields": len(quote), "candles": len(candles),
            "firstCandle": published_days[0] if candles else None, "windowStart": start.isoformat(),
            "corePriceRows": len(daily), "corePriceRowsBeforeWindow": earlier,
            "factorRows": len(factors), "differences": dict(errors)}


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        provider = StocksDatabaseProvider(SnapshotRepository(factory), factory)
        service = WorkbenchService(provider)
        products = session.execute(text("""
            select product, (as_of at time zone 'UTC')::date as day, version from mart.published_snapshot
            where product like 'stock-workbench:%' order by product
        """)).all()
        assert products
        report = {p.split(":", 1)[1]: inspect_symbol(session, service, p.split(":", 1)[1], day, version)
                  for p, day, version in products}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return int(any(row["differences"] for row in report.values()))


if __name__ == "__main__":
    raise SystemExit(main())
