"""把同版财报可见版本与逐日股本发布给组合，随现有行情重建更新。"""
from datetime import date
from math import isfinite

from app.db.models import DailyBasic
from app.stocks.api.datasets import daily_rows_query
from app.stocks.api.financial import load_attribution_earnings


def load_attribution_inputs(factory, starts, as_of):
    with factory() as session:
        earnings = load_attribution_earnings(session, tuple(starts), as_of)
        assets = {symbol: dict(earnings=[], shares=[]) for symbol in starts}
        for symbol, timeline in earnings.items():
            before = [entry for entry in timeline if entry["knownAt"] < starts[symbol]]
            assets[symbol]["earnings"] = before[-1:] + [
                entry for entry in timeline if entry["knownAt"] >= starts[symbol]]
        if starts:
            query = daily_rows_query(DailyBasic, date.fromisoformat(min(starts.values())), as_of,
                tuple(starts)).order_by(DailyBasic.ts_code, DailyBasic.trade_date)
            for row in session.scalars(query):
                raw = row.data.get("total_share")
                try:
                    shares = float(raw) * 10000 if not isinstance(raw, bool) else 0
                except (TypeError, ValueError, OverflowError):
                    shares = 0
                if row.trade_date.isoformat() >= starts[row.ts_code]:
                    assets[row.ts_code]["shares"].append(dict(date=row.trade_date.isoformat(),
                        value=shares if isfinite(shares) and shares > 0 else None,
                        sourceId=f"core.daily_basic:{row.id}"))
        return dict(asOf=as_of.isoformat(), method="ttm-profit-per-current-share", assets=assets)


def valid_attribution_inputs(value, symbols, as_of):
    def day(item):
        try:
            return isinstance(item, str) and date.fromisoformat(item).isoformat() == item and item <= as_of
        except ValueError:
            return False

    def amount(item):
        return item is None or type(item) in (int, float) and isfinite(item)

    if not isinstance(value, dict) or value.get("asOf") != as_of or value.get("method") != "ttm-profit-per-current-share":
        return False
    assets = value.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(symbols):
        return False
    for asset in assets.values():
        if not isinstance(asset, dict) or not isinstance(asset.get("earnings"), list) or not isinstance(asset.get("shares"), list):
            return False
        dates = []
        for row in asset["earnings"]:
            if not isinstance(row, dict) or "profitTtm" not in row or not day(row.get("knownAt")) or not day(row.get("periodEnd")):
                return False
            if row["periodEnd"] > row["knownAt"] or not amount(row.get("profitTtm")) or not isinstance(row.get("reason"), str):
                return False
            if "reportDisclosedAt" in row and (not day(row["reportDisclosedAt"])
                    or not row["periodEnd"] <= row["reportDisclosedAt"] <= row["knownAt"]):
                return False
            if not isinstance(row.get("sourceIds"), list) or not row["sourceIds"] or any(not isinstance(s, str) or not s for s in row["sourceIds"]):
                return False
            dates.append(row["knownAt"])
        if dates != sorted(set(dates)):
            return False
        dates = []
        for row in asset["shares"]:
            if not isinstance(row, dict) or "value" not in row or not day(row.get("date")) or not amount(row.get("value")):
                return False
            if not isinstance(row.get("sourceId"), str) or not row["sourceId"]:
                return False
            if row["value"] is not None and row["value"] <= 0:
                return False
            dates.append(row["date"])
        if dates != sorted(set(dates)):
            return False
    return True
