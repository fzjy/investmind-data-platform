from __future__ import annotations

from collections.abc import Mapping
from app.portfolio.payload_validation import validate_portfolio
from app.stocks.payload_validation import _validate_screener, _validate_workbench
from app.market.payload_validation import _validate_market_explorer, _validate_market_overview
from app.macro.payload_validation import _validate_macro_insights, _validate_macro_transmission


_REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "screener": ("bootstrap.groups", "bootstrap.thresholds", "bootstrap.categoryValues", "stocks"),
    "stock-workbench": ("sidebar.stocks", "quote.intraday", "dailyChart.candles", "metrics.cards", "metrics.fields", "risks.groups", "financial.dashboard.products", "financial.shareholders.holders", "financial.statements.tables"),
    "market-overview": ("marketSnapshot.asOf", "turnoverSnapshot.value", "limitStats.riseLimit", "sentimentSnapshot.score", "breadthSnapshots"),
    "market-explorer": ("heatmapCategories", "heatmapData", "rankingLeaders", "rankingDecliners"),
    "macro-transmission": ("transmission.weather", "transmission.modes", "transmission.industries", "analysis.cards"),
    "macro-insights": ("compassPoints", "sandboxGroups", "trendDefinitions"),
    "portfolio": ("assets", "prices", "defaultState.positions"),
}


def validate_product_payload(product: str, payload: object) -> tuple[str, ...]:
    base = product.split(":", 1)[0]
    if not isinstance(payload, Mapping):
        return ("$",)
    missing: list[str] = []
    for path in _REQUIRED_PATHS.get(base, ()):
        current: object = payload
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                missing.append(path)
                break
            current = current[part]
    if base == "stock-workbench":
        _validate_workbench(payload, missing)
    elif base == "market-explorer":
        _validate_market_explorer(payload, missing)
    elif base == "market-overview":
        _validate_market_overview(payload, missing)
    elif base == "screener":
        _validate_screener(payload, missing)
    elif base == "portfolio":
        validate_portfolio(payload, missing)
    elif base == "macro-transmission":
        _validate_macro_transmission(payload, missing)
    elif base == "macro-insights":
        _validate_macro_insights(payload, missing)
    return tuple(missing)


__all__ = ["validate_product_payload"]
