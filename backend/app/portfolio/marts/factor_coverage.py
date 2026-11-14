"""按实际日线日期核对原始因子；价格回退与核验结果分别保存。"""
from dataclasses import replace

from app.stocks.api.quotes import build_adjustment_index


def index_portfolio_factors(snapshot):
    by_symbol = {}
    usable = []
    for row in snapshot.adjustment_factors:
        value = row.adj_factor
        valid = value is not None and value.is_finite() and value > 0
        by_symbol.setdefault(row.ts_code, {})[row.trade_date] = valid
        if valid:
            usable.append(row)
    return by_symbol, build_adjustment_index(replace(snapshot, adjustment_factors=tuple(usable)))


def factor_coverage(rows, observations):
    dates = sorted(row.trade_date for row in rows)
    base_date = max(observations, default=dates[-1])
    return {
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "checked": True,
        "baseValid": base_date >= dates[-1] and observations.get(base_date, False),
        "issues": [
            {"date": day.isoformat(), "reason": "invalid" if day in observations else "missing"}
            for day in dates if not observations.get(day)
        ],
    }
