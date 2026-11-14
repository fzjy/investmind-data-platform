from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.db.models import DailyPrice, Security
from app.stocks.derived.pipeline import DerivedReport
from app.jobs.build_and_publish import BuildAndPublishJob
from app.marts.contracts import MartRows
from app.marts.quality import QualityGate
from app.stocks.marts.stocks import ScreenerMartBuilder
from backend.tests.marts.test_screener_builder import _core_snapshot


def results(snapshot, candidate):
    context = BuildAndPublishJob._quality_context(snapshot, (), DerivedReport((), 0, 0, 0, 0))
    return {item.name: item for item in QualityGate().evaluate((candidate,), context)}


@pytest.mark.parametrize("failure", ["omit", "extra"])
def test_equal_payload_rows_cannot_hide_wrong_expected_screener_scope(failure):
    snapshot = _core_snapshot()
    screener = ScreenerMartBuilder().build(snapshot, "test")
    rows = [] if failure == "omit" else [*deepcopy(screener.payload["stocks"]),
        {**deepcopy(screener.payload["stocks"][0]), "symbol": "000001.SZ"}]
    candidate = replace(screener, payload={**screener.payload, "stocks": rows}, rows=MartRows(screener=tuple(rows)))
    result = results(snapshot, candidate).get("screener-expected-scope")
    assert result is not None and not result.passed and result.severity == "blocking"


def test_global_master_count_is_not_expected_price_coverage():
    snapshot = _core_snapshot()
    # 无行情证券不能抬高预期选股行数；包括未来日期/未请求工作台。
    no_price = Security(ts_code="000001.SZ", symbol="000001", name="sample", exchange="SZSE")
    snapshot = replace(snapshot, securities=(*snapshot.securities, no_price))
    result = results(snapshot, ScreenerMartBuilder().build(snapshot, "test"))
    assert result["screener-expected-scope"].passed
    assert result["screener-expected-scope"].details["expectedCount"] == 1


def test_latest_missing_close_and_empty_range_are_legal():
    snapshot = _core_snapshot()
    latest = DailyPrice(ts_code="600519.SH", trade_date=snapshot.as_of, close=None)
    snapshot = replace(snapshot, daily_prices=(latest,), securities=())
    result = results(snapshot, ScreenerMartBuilder().build(snapshot, "test"))
    assert result["screener-expected-scope"].passed


def test_nullable_coverage_reports_counts_without_blocking():
    snapshot = _core_snapshot()
    screener = ScreenerMartBuilder().build(snapshot, "test")
    screener.payload["stocks"][0]["pe"] = None
    result = results(snapshot, screener).get("screener-field-coverage")
    assert result is not None and result.severity == "advisory"
    assert result.details["fields"]["pe"] == {"present": 0, "total": 1}
    assert result.details["fields"]["price"] == {"present": 1, "total": 1}


def test_expected_scope_uses_last_visible_close_not_any_historical_price():
    snapshot = _core_snapshot()
    snapshot = replace(snapshot, as_of=date(2026, 8, 25), daily_prices=(*snapshot.daily_prices,
        DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 25), close=None)))
    candidate = ScreenerMartBuilder().build(snapshot, "test")
    result = results(snapshot, candidate).get("screener-expected-scope")
    assert result is not None and result.passed
    assert result.details["expectedCount"] == 0


def test_first_active_security_without_quotes_is_valid_explicit_empty_scope():
    snapshot = replace(_core_snapshot(), daily_prices=(), daily_basics=())
    candidate = ScreenerMartBuilder().build(snapshot, "first-empty")
    quality = results(snapshot, candidate)
    assert candidate.payload["stocks"] == []
    assert quality["screener-expected-scope"].details["expectedCount"] == 0
    assert all(item.passed for item in quality.values() if item.severity == "blocking")
