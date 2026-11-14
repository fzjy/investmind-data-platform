from __future__ import annotations

from app.shared.validation import require_object


MARKET_OVERVIEW_KEYS = {
    "marketSnapshot",
    "marketStatuses",
    "globalIndices",
    "turnoverSnapshot",
    "turnoverTop10",
    "limitStats",
    "sentimentSnapshot",
    "fundFlowSnapshot",
    "breadthSnapshots",
    "etfQuotes",
}
MARKET_EXPLORER_KEYS = {
    "commodityCategories",
    "commodityData",
    "commodityPeriods",
    "heatmapCategories",
    "heatmapData",
    "marketNews",
    "rankingDecliners",
    "rankingLeaders",
    "snapshotLabel",
}


def normalize_market_overview(value: object) -> dict[str, object]:
    return require_object(value, MARKET_OVERVIEW_KEYS)


def normalize_market_explorer(value: object) -> dict[str, object]:
    return require_object(value, MARKET_EXPLORER_KEYS)
