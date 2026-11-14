from __future__ import annotations

from app.shared.validation import require_object


PORTFOLIO_KEYS = {"assets", "defaultState", "prices", "snapshotLabel", "storageKey"}


def normalize_portfolio(value: object) -> dict[str, object]:
    return require_object(value, PORTFOLIO_KEYS)
