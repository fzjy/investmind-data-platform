from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.fred import FredMacroProvider
from app.connectors.macro.transport import MacroHttpTransport
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


def level(period, value, start="2025-02-01", end="9999-12-31"):
    return {"date": period, "value": value, "realtime_start": start, "realtime_end": end}


def fetch(series_id, rows, *, page_size=100000):
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    series = catalog.require(series_id)
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("vintagedates"):
            return httpx.Response(200, json={"count": 3, "offset": 0,
                "vintage_dates": ["2025-02-01", "2025-02-10", "2025-02-12"]})
        # Reproduce the actual failed historical transformation request.
        if request.url.params["units"] != "lin":
            return httpx.Response(400, json={"error_code": 400})
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"count": len(rows), "offset": offset,
            "units": "lin", "output_type": 1,
            "observations": rows[offset:offset + page_size]})

    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handle)),
                            max_attempts=1, backoff_seconds=(0,)) as transport:
        result = FredMacroProvider("controlled-key", transport=transport).fetch(
            series, series.sources[0], MacroFetchWindow(date(2025, 1, 1), date(2025, 2, 28),
                                                       datetime(2025, 2, 15, tzinfo=UTC)))
    return result, requests


@pytest.mark.parametrize("series_id", ["us.cpi.yoy", "us.cpi.core_yoy", "us.pce.core_yoy"])
def test_yoy_uses_base_visible_at_each_revision_not_latest_base(series_id):
    result, requests = fetch(series_id, [
        level("2024-01-01", "100", end="2025-02-09"),
        level("2024-01-01", "125", "2025-02-10", "2025-02-15"),
        level("2024-01-01", "200", "2025-02-16"),
        level("2025-01-01", "150", end="2025-02-11"),
        level("2025-01-01", "175", "2025-02-12"),
    ], page_size=2)
    assert result.status == "success"
    assert [(row.observed_at, row.revision, row.value) for row in result.observations] == [
        (date(2025, 1, 1), "2025-02-01", Decimal("50")),
        (date(2025, 1, 1), "2025-02-10", Decimal("20")),
        (date(2025, 1, 1), "2025-02-12", Decimal("40")),
    ]
    assert requests[1].url.params["observation_start"] == "2024-01-01"
    assert all(row.released_at <= datetime(2025, 2, 15, tzinfo=UTC) for row in result.observations)
    assert result.request_count == 4
    assert len(result.raw_payload["pages"]) == 4


def test_monthly_change_recomputes_when_previous_month_is_revised():
    result, requests = fetch("us.labor.nonfarm_payroll_change", [
        level("2024-12-01", "200", end="2025-02-09"),
        level("2024-12-01", "201", "2025-02-10"),
        level("2025-01-01", "205", end="2025-02-11"),
        level("2025-01-01", "208", "2025-02-12"),
    ])
    assert result.status == "success"
    assert [(row.revision, row.value) for row in result.observations] == [
        ("2025-02-01", Decimal("5")), ("2025-02-10", Decimal("4")), ("2025-02-12", Decimal("7"))]
    assert requests[1].url.params["observation_start"] == "2024-12-01"


def test_missing_base_month_does_not_use_positional_previous_row():
    result, _ = fetch("us.cpi.yoy", [level("2023-12-01", "100"), level("2025-01-01", "150")])
    assert result.status == "no_change"
    assert result.observations == ()


def test_zero_yoy_base_fails_without_partial_observations():
    result, _ = fetch("us.cpi.yoy", [level("2024-01-01", "0"), level("2025-01-01", "150")])
    assert result.status == "failed"
    assert result.observations == ()
    assert result.error.code == "FRED_TRANSFORM_ZERO_BASE"
    assert result.request_count == 2
    assert len(result.raw_payload["pages"]) == 2


def test_missing_value_revision_does_not_resurrect_expired_base():
    result, _ = fetch("us.cpi.yoy", [
        level("2024-01-01", "100", end="2025-02-09"),
        level("2024-01-01", ".", "2025-02-10"),
        level("2025-01-01", "150", end="2025-02-11"),
        level("2025-01-01", "175", "2025-02-12"),
    ])
    assert result.status == "success"
    assert [(row.revision, row.value) for row in result.observations] == [("2025-02-01", Decimal("50"))]


def test_conflicting_active_base_intervals_are_rejected():
    result, _ = fetch("us.cpi.yoy", [
        level("2024-01-01", "100"), level("2024-01-01", "125", "2025-02-10"),
        level("2025-01-01", "150"),
    ])
    assert result.status == "failed"
    assert result.observations == ()
    assert result.error.code == "FRED_REALTIME_CONFLICT"


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity"])
def test_nonfinite_base_is_rejected(bad_value):
    result, _ = fetch("us.cpi.yoy", [level("2024-01-01", bad_value), level("2025-01-01", "150")])
    assert result.status == "failed"
    assert result.observations == ()
    assert result.error.code == "FRED_VALUE_INVALID"
