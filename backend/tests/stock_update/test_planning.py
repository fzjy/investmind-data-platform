from datetime import date, datetime, timezone, timedelta

import pytest

from app.config import Settings
from app.db.models import Security, TradeCalendar, DailyPrice, DisclosureSchedule, IncomeStatement
from app.stocks.universes import UniverseResolver



def test_config_budget_rejects_zero():
    with pytest.raises(ValueError):
        Settings(stock_update_max_requests_per_run=0)


def fixtures(new_pg_factory):
    factory = new_pg_factory()
    selection = UniverseResolver().resolve('representative-50')
    with factory.begin() as s:
        s.add_all([Security(ts_code=c, symbol=c[:6], name='样本', exchange='SSE', list_date=date(2026, 10, 12)) for c in selection.symbols])
    return factory, selection


def calendar(factory, start=date(2023, 10, 13), end=date(2026, 10, 14)):
    with factory.begin() as s:
        for exchange in ('SSE', 'SZSE'):
            day = start
            while day <= end:
                s.add(TradeCalendar(exchange=exchange, cal_date=day, is_open=day.weekday() < 5))
                day += timedelta(days=1)


def test_calendar_pending_never_selects_stale_date(new_pg_factory):
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    f, pool = fixtures(new_pg_factory)
    with f.begin() as s:
        s.add(TradeCalendar(exchange='SSE', cal_date=date(2026, 9, 18), is_open=True))
    p = StockUpdatePlanner(f, pool, StockUpdateOptions())
    result = p.preflight(datetime(2026, 10, 14, 0, tzinfo=timezone.utc))
    assert result['upperBound'] == '2026-10-13'
    assert result['asOf'] is None and result['calendarPending'] is True


def test_market_inventory_diff_and_financial_period_semantics(new_pg_factory):
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    f, pool = fixtures(new_pg_factory)
    calendar(f)
    symbol = pool.symbols[0]
    with f.begin() as s:
        s.add(DailyPrice(ts_code=symbol, trade_date=date(2026, 10, 12), close=10))
        s.add(DisclosureSchedule(ts_code=symbol, end_date=date(2026, 9, 30), ann_date=date(2026, 10, 10), actual_date=date(2026, 10, 12)))
        s.add(IncomeStatement(ts_code=symbol, end_date=date(2026, 9, 30), ann_date=date(2026, 10, 12), data={}))
    p = StockUpdatePlanner(f, pool, StockUpdateOptions())
    plans = p.plans(date(2026, 10, 13))
    daily = next(x for x in plans if x.api_name == 'daily' and x.params['ts_code'] == symbol)
    assert daily.params['start_date'] == '20261013'
    financial = [x for x in plans if 'period' in x.params]
    assert {x.api_name for x in financial} == {'income', 'balancesheet', 'cashflow', 'fina_indicator'}
    assert {'q_eps', 'q_roe', 'eps', 'bps'} <= set(next(p.fields for p in financial if p.api_name == 'fina_indicator'))
    assert all(x.params == {'ts_code': symbol, 'period': '20260930'} for x in financial)
    dividend = next(x for x in plans if x.api_name == 'dividend')
    assert set(dividend.params) == {'ts_code'}


def test_later_parent_and_cumulative_do_not_hide_missing_single_quarter_eps(new_pg_factory):
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    factory, pool = fixtures(new_pg_factory)
    symbol, period = pool.symbols[0], date(2026, 9, 30)
    day = date(2026, 10, 13)
    with factory.begin() as s:
        s.add(DisclosureSchedule(ts_code=symbol, end_date=period, actual_date=date(2026, 10, 8)))
        for typ, stamp, data in [('2', 7, {}), ('1', 8, {'basic_eps': 1}), ('6', 9, {'basic_eps': 3})]:
            s.add(IncomeStatement(ts_code=symbol, end_date=period, report_type=typ,
                ann_date=date(2026, 10, stamp), data=data))
    planner = StockUpdatePlanner(factory, pool, StockUpdateOptions())
    assert any(p.api_name == 'income' for p in planner.stage_plans('financial', day))
    assert not any(p.api_name == 'income' for p in planner.stage_plans('financial', day, backfill_card_fields=False))


def test_future_explicit_target_is_rejected(new_pg_factory):
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    f, pool = fixtures(new_pg_factory)
    with pytest.raises(ValueError):
        StockUpdatePlanner(f, pool, StockUpdateOptions(as_of=date(2026, 10, 14))).preflight(datetime(2026, 10, 14, 0, tzinfo=timezone.utc))


def test_cutoff_and_weekend_are_not_calendar_guesses(new_pg_factory):
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    f, pool = fixtures(new_pg_factory)
    options = StockUpdateOptions()
    assert options.upper(datetime(2026, 10, 14, 10, 59, tzinfo=timezone.utc)) == date(2026, 10, 13)
    assert options.upper(datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)) == date(2026, 10, 14)
    calendar(f, start=date(2023, 10, 16), end=date(2026, 10, 18))
    assert StockUpdatePlanner(f, pool, options).target(date(2026, 10, 18)) == date(2026, 10, 16)


def test_calendar_conflicting_or_missing_day_blocks_target(new_pg_factory):
    from sqlalchemy import delete, select
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    f, pool = fixtures(new_pg_factory)
    calendar(f)
    with f.begin() as s:
        r = s.scalar(select(TradeCalendar).where(TradeCalendar.exchange == 'SZSE', TradeCalendar.cal_date == date(2026, 10, 13)))
        r.is_open = False
    with pytest.raises(ValueError, match='calendar'):
        StockUpdatePlanner(f, pool, StockUpdateOptions()).target(date(2026, 10, 13))
    with f.begin() as s:
        s.execute(delete(TradeCalendar).where(TradeCalendar.exchange == 'SZSE', TradeCalendar.cal_date == date(2026, 10, 13)))
    with pytest.raises(ValueError, match='calendar'):
        StockUpdatePlanner(f, pool, StockUpdateOptions()).target(date(2026, 10, 13))


def test_first_weekend_calendar_request_covers_target_history_start(new_pg_factory):
    from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions
    f, pool = fixtures(new_pg_factory)
    plans = StockUpdatePlanner(f, pool, StockUpdateOptions()).calendar_plans(date(2026, 10, 18))
    assert all(p.params['start_date'] <= '20231016' for p in plans)
