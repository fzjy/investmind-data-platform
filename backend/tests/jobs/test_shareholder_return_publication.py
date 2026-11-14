from copy import deepcopy
from datetime import date
from decimal import Decimal
import pytest
from app.marts.contracts import MartCandidate
from app.marts.publication import PublicationBaseChanged
from app.stocks.marts.shareholder_returns import json_numbers, guard_returns, rebuild_shareholder_returns
from backend.tests.jobs.test_metric_card_publication import prepared, state, SYMBOL, PRODUCT


def test_collected_financial_and_paid_event_reach_the_existing_snapshot():
    from app.db.models import Dividend, IncomeStatement
    from app.stocks.marts.metric_cards import rebuild_metric_cards

    factory = prepared()
    period = date(2011, 12, 31)
    with factory.begin() as session:
        session.add(IncomeStatement(ts_code=SYMBOL, end_date=period, ann_date=date(2012, 3, 1),
            report_type="1", data={"n_income_attr_p": "9000000"}))
        session.add(Dividend(ts_code=SYMBOL, end_date=period, ann_date=date(2012, 5, 1),
            div_proc="实施", record_date=date(2012, 6, 1), ex_date=date(2012, 6, 4),
            pay_date=date(2012, 6, 6), cash_div_tax=Decimal("0.42")))
    report = rebuild_metric_cards(factory, dry_run=False, symbols=[SYMBOL], shareholder_returns=True)
    assert report["status"] == "completed"
    years = state(factory)[PRODUCT][1]["financial"]["shareholders"]["annualDistributions"]
    row = next(r for r in years if r["year"] == "2011")
    assert row["parentProfit"] == 9000000
    assert row["observedDps"] == 0.42
    assert row["payoutRatio"] is None
    assert row["events"][0]["payDate"] == "2012-06-06"
    assert rebuild_metric_cards(factory, dry_run=False, symbols=[SYMBOL], shareholder_returns=True)["status"] == "no_change"


def test_json_boundary_preserves_raw_strings():
    assert json_numbers({"amount": Decimal("1.25"), "events": [{"raw": "1.25"}]}) == {"amount": 1.25, "events": [{"raw": "1.25"}]}


def test_narrow_publish_and_retry_preserve_other_fields(monkeypatch):
    factory = prepared()
    before = state(factory)
    import app.stocks.shareholder_returns as source
    original = source.load_shareholder_returns
    def changed(session, symbols, as_of):
        values = original(session, symbols, as_of)
        for value in values.values():
            value["annualDistributions"] = [{"year": "2025", "dps": Decimal("0.5")}]
        return values
    monkeypatch.setattr(source, "load_shareholder_returns", changed)
    assert rebuild_shareholder_returns(factory, symbols=[SYMBOL])["changedSymbols"] == [SYMBOL]
    assert state(factory) == before
    assert rebuild_shareholder_returns(factory, dry_run=False, symbols=[SYMBOL])["status"] == "completed"
    after = state(factory)
    for product in before:
        if product != PRODUCT:
            assert after[product] == before[product]
    old, new = deepcopy(before[PRODUCT][1]), deepcopy(after[PRODUCT][1])
    for key in ("annualDistributions", "concentrationPeriods"):
        old["financial"]["shareholders"].pop(key, None)
        new["financial"]["shareholders"].pop(key, None)
    assert new == old
    assert rebuild_shareholder_returns(factory, dry_run=False, symbols=[SYMBOL])["status"] == "no_change"


def test_post_collection_joint_publication_and_explicit_cards_scope(monkeypatch):
    from app.stocks.marts.metric_cards import rebuild_metric_cards
    import app.stocks.shareholder_returns as source
    factory = prepared()
    original = source.load_shareholder_returns
    def changed(session, symbols, as_of):
        values = original(session, symbols, as_of)
        for value in values.values():
            value["annualDistributions"] = [{"year": "2025", "dps": Decimal("0.7")}]
        return values
    monkeypatch.setattr(source, "load_shareholder_returns", changed)
    before = state(factory)
    rebuild_metric_cards(factory, dry_run=False)
    assert state(factory) == before
    report = rebuild_metric_cards(factory, dry_run=False, shareholder_returns=True, symbols=[SYMBOL])
    assert report["status"] == "completed"
    assert state(factory)[PRODUCT][1]["financial"]["shareholders"]["annualDistributions"][0]["dps"] == 0.7
    assert rebuild_metric_cards(factory, dry_run=False, shareholder_returns=True, symbols=[SYMBOL])["status"] == "no_change"
