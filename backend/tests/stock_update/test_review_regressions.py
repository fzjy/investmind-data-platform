from dataclasses import replace
from datetime import date, timedelta
import httpx
import pytest
from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset
from app.db.models import DisclosureSchedule, IncomeStatement
from app.db.models import Classification, ClassificationMember
from app.stocks.update.repository import StockUpdateRepository
from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions, plan_for
from app.stocks.update.sync import StockUpdateSync
from backend.tests.stock_update.test_planning import fixtures
from backend.tests.stock_update.test_sync import task, NOW
from backend.tests.stock_update.test_validation import response


def test_actual_financial_empty_cannot_complete(new_pg_factory):
    f, pool = fixtures(new_pg_factory)
    symbol = pool.symbols[0]
    with f.begin() as s:
        s.add(DisclosureSchedule(ts_code=symbol, end_date=date(2026, 9, 30), ann_date=date(2026, 10, 1), actual_date=date(2026, 10, 12)))
    p = next(p for p in StockUpdatePlanner(f, pool, StockUpdateOptions()).stage_plans('financial', date(2026, 10, 13)) if p.dataset == Dataset.INCOME)
    r = TushareResponse(fields=p.fields, items=())
    result = StockUpdateRepository(f).ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13))
    assert result['status'] == 'pending'
    future = response(p, ts_code=symbol, end_date='20260930', ann_date='20261020')
    result = StockUpdateRepository(f).ingest_unit('test', p, future, date(2026, 10, 13), date(2023, 10, 13))
    assert result['status'] == 'pending'


def test_same_announcement_can_progress_to_actual_disclosure(new_pg_factory):
    f, pool = fixtures(new_pg_factory)
    symbol = pool.symbols[0]
    with f.begin() as s:
        s.add(DisclosureSchedule(ts_code=symbol, end_date=date(2026, 9, 30), ann_date=date(2026, 10, 1), actual_date=None))
    p = plan_for(Dataset.DISCLOSURE_DATE, {'ts_code': symbol})
    r = response(p, ts_code=symbol, end_date='20260930', ann_date='20261001', actual_date='20261012', modify_date='20261012')
    result = StockUpdateRepository(f).ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13))
    assert result['retained'] == 1
    with f() as s:
        assert s.scalar(select(DisclosureSchedule)).actual_date == date(2026, 10, 12)
    assert len(StockUpdatePlanner(f, pool, StockUpdateOptions()).stage_plans('financial', date(2026, 10, 13))) == 4


def test_target_empty_is_retryable_not_completed_zero_http(new_pg_factory):
    available = [False]
    def override(body, count):
        if body['api_name'] in ('daily', 'daily_basic', 'adj_factor') and available[0]:
            fields = body['fields'].split(',')
            row = {'ts_code': body['params']['ts_code'], 'trade_date': '20261013', 'close': 10, 'total_mv': 100, 'adj_factor': 1}
            return httpx.Response(200, json={'code': 0, 'data': {'fields': fields, 'items': [[row.get(f) for f in fields]]}})
    job, client, calls = task(new_pg_factory, override=override)
    try:
        first = job.run(NOW)
        assert first['status'] == 'partial' and first['publishReady'] is False
        assert first['requests'] == 103
        available[0] = True
        result = job.run(NOW)
        assert result['status'] == 'completed' and result['targetMarketComplete'] == 50
        assert result['requests'] == 150
        assert job.run(NOW)['requests'] == 0
    finally:
        client.close()


def test_resumed_preflight_uses_frozen_target(new_pg_factory):
    job, client, _ = task(new_pg_factory, budget=3)
    try:
        result = job.run(NOW)
        probe = job.preflight(NOW+timedelta(days=1))
        assert probe['upperBound'] == probe['asOf'] == '2026-10-13'
        assert probe['calendarPending'] is False
    finally:
        client.close()


@pytest.mark.parametrize("legacy", [0, 3, 1], ids=["current", "pre-sankey", "pre-metric-cards"])
def test_report_gap_blocks_ready_then_recovers_all_four_tables(new_pg_factory, monkeypatch, legacy):
    available = [False]
    def override(body, count):
        if body['api_name'] in {'income', 'balancesheet', 'cashflow', 'fina_indicator'} and available[0]:
            fields = body['fields'].split(',')
            row = {'ts_code': body['params']['ts_code'], 'end_date': body['params']['period'],
                'ann_date': '20261012', 'f_ann_date': '20261012', 'report_type': '1', 'update_flag': '0',
                'total_revenue': 100, 'n_income_attr_p': 10, 'total_assets': 100, 'roe': 10}
            return httpx.Response(200, json={'code': 0, 'data': {'fields': fields, 'items': [[row.get(f) for f in fields]]}})
    job, client, _ = task(new_pg_factory, market_ready=True, override=override)
    try:
        with job.factory.begin() as s:
            nodes = [Classification(provider='tushare', source='SW2021', classification_id=f'test{i}', level=i,
                name=f'行业{i}', parent_id=f'test{i-1}' if i>1 else None, data={}) for i in (1, 2, 3)]
            s.add_all(nodes)
            s.flush()
            s.add_all([ClassificationMember(classification_id=nodes[-1].id, ts_code=c, effective_from=date(2023, 1, 1), data={}) for c in job.selection.symbols])
            s.add(DisclosureSchedule(ts_code=job.selection.symbols[0], end_date=date(2026, 9, 30), ann_date=date(2026, 10, 1), actual_date=date(2026, 10, 12)))
        from app.connectors.tushare.strategies import FIELD_REGISTRY, LEGACY_CASHFLOW_FIELDS
        with monkeypatch.context() as original_contract:
            if legacy:
                original_contract.setitem(FIELD_REGISTRY, Dataset.CASHFLOW, LEGACY_CASHFLOW_FIELDS)
            if legacy == 1:
                for dataset, excluded in ((Dataset.INCOME, {'basic_eps'}), (Dataset.FINANCIAL_INDICATOR, {'eps', 'bps'})):
                    original_contract.setitem(FIELD_REGISTRY, dataset, tuple(f for f in FIELD_REGISTRY[dataset] if f not in excluded))
            result = job.run(NOW)
        assert result['status'] == 'partial' and result['sourceReady'] is True and result['publishReady'] is False
        assert result['stage'] == 'financial'
        available[0] = True
        recovered = job.run(NOW)
        assert recovered['requests'] == 4 and recovered['publishReady'] is True
        from app.stocks.update.publication import publish_stock_update
        report = publish_stock_update(job.factory, job.selection, date(2026, 10, 13), job.lock, collection=recovered)
        assert report.status == 'completed', report
        assert len(report.promoted_products) == 51
        assert job.contract_field_version(date.fromisoformat(result['upperBound']), result['identity']) == legacy
        if legacy == 1:
            day = date(2026, 10, 13)
            assert result['identity'] == recovered['identity']
            assert {p.dataset for p in job.planner.stage_plans('financial', day)} == {Dataset.INCOME, Dataset.FINANCIAL_INDICATOR}
            with job.factory.begin() as s:
                s.add(DisclosureSchedule(ts_code=job.selection.symbols[1], end_date=date(2026, 9, 30),
                                         ann_date=date(2026, 10, 10), actual_date=date(2026, 10, 12)))
            with pytest.raises(ValueError, match='new or remaining required financial inventory'):
                publish_stock_update(job.factory, job.selection, day, job.lock, collection=recovered)
            # 同一截止日的下一份新合同仍按扩展字段采集。
            updated = job.run(NOW)
            assert updated['identity'] != recovered['identity'] and updated['publishReady']
            with job.factory() as s:
                row = s.scalar(select(IncomeStatement).where(IncomeStatement.ts_code == job.selection.symbols[0]))
                assert 'basic_eps' in row.data
    finally:
        client.close()


def test_replaying_narrow_cashflow_keeps_same_revision_details(new_pg_factory):
    from app.db.models import CashflowStatement
    from app.connectors.tushare.strategies import LEGACY_CASHFLOW_FIELDS

    factory, pool = fixtures(new_pg_factory)
    symbol = pool.symbols[0]
    full = plan_for(Dataset.CASHFLOW, {"ts_code": symbol, "period": "20260930"})
    narrow = replace(full, fields=LEGACY_CASHFLOW_FIELDS)
    repo = StockUpdateRepository(factory)
    common = dict(ts_code=symbol, end_date="20260930", ann_date="20261012",
                  f_ann_date="20261012", report_type="1", comp_type="1", update_flag="1")
    for plan, vals in [(narrow, {}), (full, {"net_cash_rece_sec": -5, "c_prepay_amt_borr": 0}),
                       (narrow, {"n_cashflow_act": 100})]:
        result = repo.ingest_unit("test", plan, response(plan, **common, **vals),
                                  date(2026, 10, 13), date(2023, 10, 13))
        assert result["status"] == "completed" and result["retained"] == 1
    with factory() as session:
        row = session.scalar(select(CashflowStatement).where(CashflowStatement.ts_code == symbol))
        assert row.data["net_cash_rece_sec"] == -5 and row.data["c_prepay_amt_borr"] == 0
        assert row.data["n_cashflow_act"] == 100


def test_daily_cashflow_parent_report_does_not_satisfy_disclosure(new_pg_factory):
    from app.db.models import CashflowStatement
    from app.connectors.tushare.strategies import LEGACY_CASHFLOW_FIELDS

    factory, pool = fixtures(new_pg_factory)
    code, period, announced, target = pool.symbols[0], date(2026, 9, 30), date(2026, 10, 12), date(2026, 10, 13)
    with factory.begin() as session:
        session.add(DisclosureSchedule(ts_code=code, end_date=period, actual_date=announced))
        session.add(CashflowStatement(ts_code=code, end_date=period, ann_date=announced, report_type="6", data={}))
    planner = StockUpdatePlanner(factory, pool, StockUpdateOptions())
    plan = next(p for p in planner.stage_plans("financial", target) if p.dataset == Dataset.CASHFLOW)
    assert plan.api_name == "cashflow" and plan.params == {"ts_code": code, "period": "20260930"}
    assert all(p.dataset != Dataset.CASHFLOW for p in planner.stage_plans("financial", target, legacy_cashflow=True))
    with factory() as session:
        assert not StockUpdateRepository.requirement_satisfied(session, plan, target)
        assert StockUpdateRepository.requirement_satisfied(session, replace(plan, fields=LEGACY_CASHFLOW_FIELDS), target)
    with factory.begin() as session:
        session.add(CashflowStatement(ts_code=code, end_date=period, ann_date=announced, report_type="1", data={}))
    assert all(p.dataset != Dataset.CASHFLOW for p in planner.stage_plans("financial", target))
