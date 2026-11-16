from datetime import date
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import Security, DailyPrice
from app.jobs.locks import ProcessLocalJobLock
from app.stocks.universes import UniverseResolver

from backend.tests.marts.test_screener_builder import _core_snapshot


def test_stock_builder_scope_excludes_unrelated_products():
    from app.stocks.update.publication import StockMartBuilders
    snapshot = _core_snapshot()
    codes = tuple(r.ts_code for r in snapshot.securities)
    candidates = StockMartBuilders(None).build(snapshot, codes, 'pending', workbench_symbols=codes)
    assert {c.product for c in candidates} == {'screener', *(f'stock-workbench:{c}' for c in codes)}


def test_repository_narrows_master_without_modifying_core(pg_factory):
    from app.stocks.update.publication import StockSnapshotRepository
    f = pg_factory
    with f.begin() as s:
        s.add_all([Security(ts_code=c, symbol=c[:6], name='样本', status='L') for c in ('600519.SH', '000001.SZ')])
        s.flush()
        s.add_all([DailyPrice(ts_code=c, trade_date=date(2026, 10, 13), close=10) for c in ('600519.SH', '000001.SZ')])
    result = StockSnapshotRepository(f).load(date(2026, 10, 13), ('600519.SH',))
    assert [r.ts_code for r in result.securities] == ['600519.SH']
    assert [r.ts_code for r in result.daily_prices] == ['600519.SH']
    with f() as s:
        assert len(s.scalars(select(Security)).all()) == 2


def test_incomplete_stage_never_calls_publisher(pg_factory):
    from app.stocks.update.publication import publish_stock_update
    with pytest.raises(ValueError, match='collection'):
        publish_stock_update(pg_factory, UniverseResolver().resolve('representative-50'),
            date(2026, 10, 13), ProcessLocalJobLock(), collection={'status': 'failed', 'publishReady': True})

def test_routine_update_rebuilds_list_risks_from_visible_balance_revision(pg_factory):
    from app.db.models import BalanceSheet
    from app.stocks.update.publication import StockMartBuilders, StockSnapshotRepository
    from app.stocks.derived.workbench_cards import DEBT_FIELDS
    factory = pg_factory
    code = '600519.SH'
    period, as_of = date(2025,12,31), date(2026,8,24)
    base = dict.fromkeys(DEBT_FIELDS, 0) | dict(money_cap=50, total_assets=100)
    def balance(day, goodwill):
        return BalanceSheet(ts_code=code, end_date=period, report_type='1', ann_date=day, data=base | {'goodwill':goodwill})
    with factory.begin() as s:
        s.add(Security(ts_code=code, symbol=code[:6], name='样本', exchange='SSE',status='L'))
        s.flush()
        s.add_all([DailyPrice(ts_code=code, trade_date=as_of, close=10),
                   balance(date(2026,4,2), 20), balance(date(2026,9,1), 10)])
    def build():
        snapshot = StockSnapshotRepository(factory).load(as_of, (code,))
        return StockMartBuilders(factory).build(snapshot, (code,), 'pending')[0].payload['stocks'][0]
    initial = build()
    assert initial['listRisks'][1]['status'] == 'normal' and initial == build()
    with factory.begin() as s:
        s.add(balance(date(2026,8,21), 35.01))
    revised = build()
    assert revised['listRisks'][1]['status'] == 'danger' and '35.01%' in revised['listRisks'][1]['detail']
