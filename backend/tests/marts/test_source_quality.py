from dataclasses import replace
from datetime import date
import json

import pytest

from app.db.models import Classification, ClassificationMember, TradeCalendar
from app.stocks.derived.inputs import FinancialPoint
from app.jobs.build_and_publish import BuildAndPublishJob
from app.stocks.derived.pipeline import DerivedReport
from app.marts.quality import QualityGate
from backend.tests.marts.test_screener_builder import _core_snapshot


def evaluate(snapshot):
    context = BuildAndPublishJob._quality_context(snapshot, (), DerivedReport((), 0, 0, 0, 0))
    return {result.name: result for result in QualityGate().evaluate((), context)}


def calendar(snapshot, is_open=True, exchange="SSE"):
    return replace(snapshot, trade_calendar=(TradeCalendar(exchange=exchange, cal_date=date(2026, 8, 24), is_open=is_open),))


@pytest.mark.parametrize("collection", ["daily_prices", "daily_basics", "adjustment_factors"])
def test_known_closed_day_blocks_source_market_rows(collection):
    snapshot = _core_snapshot()
    if collection == "adjustment_factors":
        from app.db.models import AdjustmentFactor
        snapshot = replace(snapshot, adjustment_factors=(AdjustmentFactor(ts_code="600519.SH", trade_date=snapshot.as_of, adj_factor=1),))
    snapshot = calendar(snapshot, False)
    result = evaluate(snapshot).get("source-trading-days")
    assert result is not None and not result.passed and result.severity == "blocking"
    assert any(issue["collection"] == collection for issue in result.details["issues"])
    json.dumps(result.details, allow_nan=False)


def test_missing_calendar_is_advisory_not_invented_open_day():
    results = evaluate(_core_snapshot())
    assert "source-calendar-coverage" in results
    assert not results["source-calendar-coverage"].passed
    assert results["source-calendar-coverage"].severity == "advisory"
    assert results["source-trading-days"].passed


def test_known_open_and_irrelevant_exchange_are_not_confused():
    snapshot = calendar(_core_snapshot())
    assert evaluate(snapshot)["source-trading-days"].passed
    snapshot = replace(snapshot, trade_calendar=(*snapshot.trade_calendar,
        TradeCalendar(exchange="SZSE", cal_date=snapshot.as_of, is_open=False)))
    assert evaluate(snapshot)["source-trading-days"].passed


def test_conflicting_calendar_records_do_not_hide_closed_evidence():
    snapshot = calendar(_core_snapshot())
    snapshot = replace(snapshot, trade_calendar=(*snapshot.trade_calendar,
        TradeCalendar(exchange="SSE", cal_date=snapshot.as_of, is_open=False)))
    assert not evaluate(snapshot)["source-trading-days"].passed


@pytest.mark.parametrize("end,visible", [(date(2027, 1, 1), date(2026, 8, 1)),
                                        (date(2025, 12, 31), date(2026, 9, 1))])
def test_financial_date_relationship_blocks_visible_future_report(end, visible):
    point = FinancialPoint(end, visible, "1", {}, "600519.SH", "core.income_statement", "sample")
    snapshot = replace(_core_snapshot(), financial_points=(point,))
    result = evaluate(snapshot).get("source-financial-dates")
    assert result is not None and not result.passed


def test_valid_financial_and_absent_disclosure_remain_distinct():
    snapshot = replace(_core_snapshot(), financial_points=(
        FinancialPoint(date(2025, 12, 31), date(2026, 3, 1), "1", {}),
        FinancialPoint(date(2025, 12, 31), None, "1", {})))
    assert evaluate(snapshot)["source-financial-dates"].passed
    assert not evaluate(snapshot)["source-financial-coverage"].passed


@pytest.mark.parametrize("failure", ["unknown-class", "unknown-security", "inverted", "cycle", "parent-level"])
def test_classification_references_and_intervals_are_checked(failure):
    root = Classification(id=1, provider="sample", classification_id="A", name="A", level=1)
    child = Classification(id=2, provider="sample", classification_id="B", name="B", level=2, parent_id="A")
    member = ClassificationMember(classification_id=2, ts_code="600519.SH", effective_from=date(2026, 1, 1))
    if failure == "unknown-class":
        member.classification_id = 99
    elif failure == "unknown-security":
        member.ts_code = "000001.SZ"
    elif failure == "inverted":
        member.effective_to = date(2025, 12, 31)
    elif failure == "cycle":
        root.parent_id = "B"
    else:
        child.level = 1
    snapshot = replace(_core_snapshot(), classifications=(root, child), classification_members=(member,))
    result = evaluate(snapshot).get("source-classification-valid")
    assert result is not None and not result.passed


def test_expired_members_and_equal_codes_in_different_providers_are_legal():
    classes = (Classification(id=1, provider="a", classification_id="A", name="A", level=1),
               Classification(id=2, provider="b", classification_id="A", name="A", level=1))
    historical = ClassificationMember(classification_id=1, ts_code="600519.SH", effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31))
    snapshot = replace(_core_snapshot(), classifications=classes, classification_history=(historical,))
    assert evaluate(snapshot)["source-classification-valid"].passed
