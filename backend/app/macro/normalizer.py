from __future__ import annotations

from app.shared.validation import require_object


MACRO_TRANSMISSION_KEYS = {"transmission", "analysis"}
MACRO_INSIGHTS_KEYS = {
    "compassPoints",
    "sandboxGroups",
    "sandboxIndicators",
    "sandboxMonths",
    "sandboxPresets",
    "statusCards",
    "trendDefinitions",
}


def normalize_macro_transmission(value: object) -> dict[str, object]:
    return require_object(value, MACRO_TRANSMISSION_KEYS)


def normalize_macro_insights(value: object) -> dict[str, object]:
    return require_object(value, MACRO_INSIGHTS_KEYS)
