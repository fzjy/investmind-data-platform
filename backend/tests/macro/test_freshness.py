from datetime import date

import pytest

from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.freshness import observation_expired


@pytest.mark.parametrize(
    ("series_id", "observed", "as_of", "expired"),
    [
        ("us.pce.core_yoy", date(2026, 7, 1), date(2026, 9, 30), False),
        ("cn.cpi.core_yoy", date(2026, 8, 1), date(2026, 11, 1), False),
        ("cn.cpi.core_yoy", date(2026, 8, 1), date(2026, 11, 2), True),
        ("cn.cpi.core_yoy", date(2024, 2, 1), date(2024, 5, 1), False),
        ("cn.cpi.core_yoy", date(2024, 2, 1), date(2024, 5, 2), True),
        ("cn.cpi.core_yoy", date(2025, 12, 1), date(2026, 3, 3), False),
        ("cn.cpi.core_yoy", date(2025, 12, 1), date(2026, 3, 4), True),
        ("cn.cpi.core_yoy", date(2026, 10, 1), date(2026, 9, 30), True),
    ],
)
def test_monthly_age_uses_end_of_statistical_period(
    series_id: str, observed: date, as_of: date, expired: bool,
) -> None:
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(series_id)
    assert observation_expired(series, observed, as_of) is expired
    assert observed.day == 1
