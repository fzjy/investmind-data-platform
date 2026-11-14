"""非有限值不能越过 DTO/OHLC 门禁，也不能替换上一成功快照。"""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from app.db.models import PageSnapshot, QualityCheckResult
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.jobs.build_history import SqlBuildHistory
from app.marts.contracts import QualityContext
from app.marts.dto import validate_product_payload
from app.marts.publication import QualityCheckFailed, SnapshotPublisher
from app.marts.quality import QualityGate
from app.stocks.marts.stocks import ScreenerMartBuilder, StockWorkbenchMartBuilder
from backend.tests.jobs.test_build_and_publish_integration import _factory
from backend.tests.marts.test_screener_builder import _core_snapshot


NONFINITE = [
    pytest.param(float("nan"), id="float-nan"),
    pytest.param(float("inf"), id="float-positive-infinity"),
    pytest.param(float("-inf"), id="float-negative-infinity"),
]
DECIMAL_NONFINITE = [
    pytest.param(Decimal("NaN"), id="decimal-nan"),
    pytest.param(Decimal("sNaN"), id="decimal-signaling-nan"),
    pytest.param(Decimal("Infinity"), id="decimal-positive-infinity"),
    pytest.param(Decimal("-Infinity"), id="decimal-negative-infinity"),
]


def candidates():
    snapshot = _core_snapshot()
    return (ScreenerMartBuilder().build(snapshot, "probe"),
            StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "probe"))


def checks(items):
    return QualityGate().evaluate(items, QualityContext(1, ("600519.SH",), 0, 0, ()))


@pytest.mark.parametrize("value", NONFINITE)
@pytest.mark.parametrize("field", ["price", "profitGrowth", "pe"])
def test_screener_required_and_nullable_numbers_reject_nonfinite(field, value):
    screener, _ = candidates()
    screener.payload["stocks"][0][field] = value
    assert f"stocks[0].{field}" in validate_product_payload("screener", screener.payload)


@pytest.mark.parametrize("value", NONFINITE)
@pytest.mark.parametrize("path", [("quote", "previousClose"), ("quote", "pe"), ("candle", "volume")])
def test_nested_workbench_numbers_reject_nonfinite(path, value):
    _, workbench = candidates()
    section, field = path
    if section == "candle":
        workbench.payload["dailyChart"]["candles"][0][field] = value
        expected = f"dailyChart.candles[0].{field}"
    else:
        workbench.payload[section][field] = value
        expected = f"{section}.{field}"
    assert expected in validate_product_payload(workbench.product, workbench.payload)


@pytest.mark.parametrize("value", [0, -1, -1.25, 1e308, None])
def test_nullable_numbers_keep_finite_negative_zero_and_missing(value):
    screener, _ = candidates()
    screener.payload["stocks"][0]["profitGrowth"] = value
    assert validate_product_payload("screener", screener.payload) == ()


@pytest.mark.parametrize("value", [True, False, None])
def test_required_price_keeps_rejecting_boolean_and_missing(value):
    screener, _ = candidates()
    screener.payload["stocks"][0]["price"] = value
    assert "stocks[0].price" in validate_product_payload("screener", screener.payload)


@pytest.mark.parametrize("value", [*NONFINITE, *DECIMAL_NONFINITE])
@pytest.mark.parametrize("location", ["rows", "payload"])
def test_nonfinite_ohlc_returns_blocking_result_with_json_safe_details(location, value):
    screener, workbench = candidates()
    candle = {**workbench.payload["dailyChart"]["candles"][0],
              **dict.fromkeys(("open", "high", "low", "close"), value)}
    if location == "rows":
        workbench = replace(workbench, rows=replace(workbench.rows, charts=(candle,)))
    else:
        workbench.payload["dailyChart"]["candles"] = [candle]
    result = next(item for item in checks((screener, workbench)) if item.name == "ohlc-valid")
    assert result.severity == "blocking" and not result.passed
    assert result.details["rows"]
    json.dumps(result.details, allow_nan=False)  # 必须可记录到质量历史，不靠 JSON NaN 扩展。


@pytest.mark.parametrize("failure", ["dto", "ohlc"])
def test_real_quality_failure_preserves_both_old_pointers_and_records(failure):
    factory = _factory()
    publisher = SnapshotPublisher(factory)
    original = candidates()
    initial_quality = checks(original)
    assert all(item.passed for item in initial_quality if item.severity == "blocking")
    publisher.publish_many(original, "old-good", initial_quality)
    screener, workbench = candidates()
    if failure == "dto":
        screener.payload["stocks"][0]["price"] = float("inf")
        expected_check = "dto-keys"
    else:
        candle = {**workbench.rows.charts[0], **dict.fromkeys(("open", "high", "low", "close"), Decimal("Infinity"))}
        workbench = replace(workbench, rows=replace(workbench.rows, charts=(candle,)))
        expected_check = "ohlc-valid"
    items = (screener, workbench)
    quality = checks(items)
    assert any(item.name == expected_check and not item.passed for item in quality)
    history = SqlBuildHistory(factory)
    run_id = history.start(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))
    history.record_quality(run_id, quality)
    with pytest.raises(QualityCheckFailed):
        publisher.publish_many(items, "new-invalid", quality)
    with factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version))) == {"old-good"}
        recorded = session.scalar(select(QualityCheckResult).where(
            QualityCheckResult.job_run_id == run_id, QualityCheckResult.check_name == expected_check))
        assert recorded is not None and not recorded.passed
        json.dumps(recorded.details, allow_nan=False)
    assert publisher.current("screener").version == "old-good"
    assert publisher.current("stock-workbench:600519.SH").version == "old-good"


@pytest.mark.parametrize("bad_values", [
    {"open": None, "high": Decimal("sNaN")},
    {"open": "bad", "high": float("inf")},
    {"open": None, "high": float("nan")},
    {"open": "bad", "high": Decimal("-Infinity")},
])
def test_mixed_bad_ohlc_paths_record_json_safe_quality_history(bad_values):
    screener, workbench = candidates()
    candle = {**workbench.rows.charts[0], **bad_values}
    workbench = replace(workbench, rows=replace(workbench.rows, charts=(candle,)))
    quality = checks((screener, workbench))
    result = next(item for item in quality if item.name == "ohlc-valid")
    assert not result.passed
    json.dumps(result.details, allow_nan=False)
    factory = _factory()
    history = SqlBuildHistory(factory)
    run_id = history.start(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))
    history.record_quality(run_id, quality)
    with factory() as session:
        recorded = session.scalar(select(QualityCheckResult).where(
            QualityCheckResult.job_run_id == run_id, QualityCheckResult.check_name == "ohlc-valid"))
        assert recorded is not None and not recorded.passed
        json.dumps(recorded.details, allow_nan=False)
