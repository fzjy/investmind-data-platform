from datetime import date

import pytest

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset, RequestPlan, FIELD_REGISTRY
from app.ingestion.normalizers import normalize_trade_calendar


def test_calendar_string_zero_is_closed():
    assert normalize_trade_calendar({'exchange': 'SSE', 'cal_date': '20261001', 'is_open': '0'})['is_open'] is False


@pytest.mark.parametrize('value', ['2', None, '', 'open', 2])
def test_calendar_unknown_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_trade_calendar({'exchange': 'SSE', 'cal_date': '20261001', 'is_open': value})


def response(plan, **row):
    return TushareResponse(fields=plan.fields, items=(tuple(row.get(f) for f in plan.fields),))


def test_future_implementation_is_not_visible():
    from app.stocks.update.validation import validate_response
    plan = RequestPlan(Dataset.DIVIDEND, 'dividend', {'ts_code': '600519.SH'},
        (*FIELD_REGISTRY[Dataset.DIVIDEND], 'imp_ann_date'), 'dividend:600519.SH')
    data = response(plan, ts_code='600519.SH', ann_date='20260501', end_date='20251231',
        div_proc='实施', imp_ann_date='20261020', cash_div=1)
    assert validate_response(plan, data, date(2026, 10, 13), date(2023, 10, 13)).items == ()


@pytest.mark.parametrize('row', [
    {'ts_code': '000001.SZ', 'trade_date': '20261013', 'adj_factor': 1},
    {'ts_code': '600519.SH', 'trade_date': 'invalid', 'adj_factor': 1},
    {'ts_code': '600519.SH', 'trade_date': '20261013', 'adj_factor': float('inf')},
])
def test_invalid_market_response_rejected(row):
    from app.stocks.update.validation import validate_response
    plan = RequestPlan(Dataset.ADJ_FACTOR, 'adj_factor', {'ts_code': '600519.SH',
        'start_date': '20261013', 'end_date': '20261013'}, FIELD_REGISTRY[Dataset.ADJ_FACTOR], 'factor')
    with pytest.raises(ValueError):
        validate_response(plan, response(plan, **row), date(2026, 10, 13), date(2023, 10, 13))


def test_conflicting_natural_keys_are_rejected():
    from app.stocks.update.validation import validate_response
    plan = RequestPlan(Dataset.ADJ_FACTOR, 'adj_factor', {'ts_code': '600519.SH'},
        FIELD_REGISTRY[Dataset.ADJ_FACTOR], 'factor')
    data = TushareResponse(fields=plan.fields, items=(('600519.SH', '20261013', 1), ('600519.SH', '20261013', 2)))
    with pytest.raises(ValueError):
        validate_response(plan, data, date(2026, 10, 13), date(2023, 10, 13))


def test_bse_calendar_uses_real_sse_evidence():
    from dataclasses import replace
    from app.db.models import Security, DailyPrice, TradeCalendar
    from app.marts.source_quality import evaluate_source_quality
    from backend.tests.marts.test_screener_builder import _core_snapshot
    snapshot = replace(_core_snapshot(), securities=(Security(ts_code='832000.BJ', symbol='832000', name='样本', exchange='BSE'),),
        daily_prices=(DailyPrice(ts_code='832000.BJ', trade_date=date(2026, 8, 24), close=10),), daily_basics=(),
        trade_calendar=(TradeCalendar(exchange='SSE', cal_date=date(2026, 8, 24), is_open=True),))
    results = {r.name: r for r in evaluate_source_quality(snapshot)}
    assert results['source-calendar-coverage'].passed is True
    assert results['source-trading-days'].passed is True


def test_bad_fields_and_cap_fail_but_empty_actions_are_legitimate():
    from app.stocks.update.planning import plan_for
    from app.stocks.update.validation import validate_response
    p = plan_for(Dataset.DIVIDEND, {'ts_code': '600519.SH'})
    empty = TushareResponse(fields=p.fields, items=())
    assert validate_response(p, empty, date(2026, 10, 13), date(2023, 10, 13)).items == ()
    with pytest.raises(ValueError, match='fields'):
        validate_response(p, TushareResponse(fields=(), items=()), date(2026, 10, 13), date(2023, 10, 13))
    with pytest.raises(ValueError, match='cap'):
        validate_response(p, TushareResponse(fields=p.fields, items=(tuple(None for _ in p.fields),)*2000), date(2026, 10, 13), date(2023, 10, 13))


def test_future_financial_raw_row_is_not_retained():
    from app.stocks.update.planning import plan_for
    from app.stocks.update.validation import validate_response
    p = plan_for(Dataset.INCOME, {'ts_code': '600519.SH', 'period': '20260930'})
    data = response(p, ts_code='600519.SH', end_date='20260930', ann_date='20261020', n_income=20)
    assert validate_response(p, data, date(2026, 10, 13), date(2023, 10, 13)).items == ()


def test_disclosure_revision_list_retains_raw_and_last_known_date():
    from app.stocks.update.planning import plan_for
    from app.stocks.update.validation import validate_response
    p = plan_for(Dataset.DISCLOSURE_DATE, {'ts_code': '600519.SH'})
    data = response(p, ts_code='600519.SH', end_date='20260930', ann_date='20261001',
        actual_date='20261012', modify_date='20261002,20261012')
    result = validate_response(p, data, date(2026, 10, 13), date(2023, 10, 13))
    assert result.rows()[0]['modify_date'] == '20261012'
    assert data.rows()[0]['modify_date'] == '20261002,20261012'
    historical = response(p, ts_code='600519.SH', end_date='20181231', ann_date='20190301', modify_date='20190509,20190510')
    assert validate_response(p, historical, date(2026, 10, 13), date(2023, 10, 13)).items == ()


def test_future_disclosure_revision_cannot_reconstruct_old_fields():
    from app.stocks.update.planning import plan_for
    from app.stocks.update.validation import validate_response
    p = plan_for(Dataset.DISCLOSURE_DATE, {'ts_code': '600519.SH'})
    data = response(p, ts_code='600519.SH', end_date='20260930', ann_date='20261001',
        actual_date='20261012', modify_date='20261002,20261020')
    assert validate_response(p, data, date(2026, 10, 13), date(2023, 10, 13)).items == ()


def test_revised_dividend_proposals_are_raw_only_not_arbitrary_cash_events():
    from app.stocks.update.planning import plan_for
    from app.stocks.update.validation import validate_response
    p = plan_for(Dataset.DIVIDEND, {'ts_code': '600519.SH'})
    base = {'ts_code': '600519.SH', 'end_date': '20251231', 'ann_date': '20260417', 'div_proc': '预案'}
    a = response(p, **base, cash_div_tax=28.02423)
    b = response(p, **base, cash_div_tax=27.993)
    raw = TushareResponse(fields=p.fields, items=a.items+b.items)
    assert validate_response(p, raw, date(2026, 10, 13), date(2023, 10, 13)).items == ()
    assert len(raw.items) == 2
    base.update(div_proc='实施', imp_ann_date='20260601')
    a = response(p, **base, cash_div_tax=28.02423)
    b = response(p, **base, cash_div_tax=27.993)
    with pytest.raises(ValueError, match='conflicting'):
        validate_response(p, TushareResponse(fields=p.fields, items=a.items+b.items), date(2026, 10, 13), date(2023, 10, 13))


def test_dividend_cash_event_intersects_window_even_with_older_report():
    from app.stocks.update.planning import plan_for
    from app.stocks.update.validation import validate_response
    p = plan_for(Dataset.DIVIDEND, {'ts_code': '600519.SH'})
    raw = response(p, ts_code='600519.SH', end_date='20230930', ann_date='20231101',
        imp_ann_date='20231110', div_proc='实施', ex_date='20231120', pay_date='20231120', cash_div_tax=10)
    assert len(validate_response(p, raw, date(2026, 10, 13), date(2023, 10, 13)).items) == 1
