"""公开事实、查询范围和 InvestMind 输入共用隔离 PostgreSQL。"""
from datetime import date, datetime, timezone
from decimal import Decimal

from app.db.models import (
    AdjustmentFactor, Classification, ClassificationMember, DailyBasic, DailyPrice,
    Dividend, IncomeStatement, Security, SecurityNameHistory, TradeCalendar,
)
from app.stocks.derived.inputs import load_metric_card_inputs
from backend.tests.database_api.test_stock_datasets import BASE, client
from backend.tests.test_dividend_ledger import SYMBOL, row, saved


DAY = date(2026, 5, 1)
WINDOW = dict(start_date='2026-05-01', end_date='2026-10-31', symbol=SYMBOL)


def get(client, dataset, **params):
    response = client.get(f'{BASE}/{dataset}', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_captured_cutoff_prevents_lookahead_and_keeps_withdrawal(saved, client):
    _, _, _, _, raw, put = saved
    put([row(), row(div_proc='停止实施', ann_date='20260620', imp_ann_date='20260620')])
    before = get(client, 'stock-dividends', **WINDOW, as_of='2026-10-30')
    assert before['data'] == [] and before['meta']['evidence_status'] == 'missing'
    after = get(client, 'stock-dividends', **WINDOW, as_of='2026-10-31')
    assert [r['div_proc'] for r in after['data']] == ['实施', '停止实施']
    assert after['data'][0]['cash_div_tax'] == '0.5'
    assert after['data'][0]['source_id'] == f'raw.source_payload:{raw.id}:row:1'
    assert not any(r['selected_implementation'] for r in after['data'])
    assert after['meta']['history_complete'] is False
    assert after['meta']['evidence_status'] == 'observed'
    put([])
    empty = get(client, 'stock-dividends', **WINDOW, as_of='2026-10-31')
    assert empty['data'] == [] and empty['meta']['evidence_status'] == 'observed'


def test_dividend_inventory_date_scope_and_conflicting_sources(saved, client):
    session, _, _, _, raw, put = saved
    put([row(), row(cash_div_tax='0.7'), row(record_date='20260710', ex_date='20260711',
        pay_date=None, div_listdate=None), row(ann_date=None, end_date='20241231')])
    session.add(Dividend(ts_code=SYMBOL, ann_date=DAY, end_date=date(2025,12,31),
                         div_proc='实施', cash_div_tax=0, data={'cash_div_tax': 99}))
    session.commit()
    data = get(client, 'stock-dividends', **WINDOW)
    assert {r['source_id'].split(':')[0] for r in data['data']} == {'raw.source_payload','core.dividend'}
    assert len(data['data']) == 4 and data['meta']['missing_announcement_rows'] == 1
    assert 'same_day_conflict' in {i['code'] for i in data['meta']['issues']}
    assert not any(r['selected_implementation'] for r in data['data'] if r['record_date']=='2026-05-10')
    assert any(r['pay_date'] is None and r['record_date']=='2026-07-10' for r in data['data'])
    raw.captured_at = datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)
    session.commit()
    early = get(client, 'stock-dividends', **WINDOW, as_of='2026-05-01')
    assert early['data'] == []  # 北京时间次日零点。


def test_evidence_pagination_integrity_and_announcements_after_cutoff(saved, client):
    session, _, _, _, raw, put = saved
    put([row(), row(record_date='20260601', ex_date='20260602', pay_date='20260603', div_listdate=None),
         row(ann_date='20261030', imp_ann_date='20261101')])
    params = dict(**WINDOW, as_of='2026-10-31', limit=1)
    first = get(client, 'stock-dividends', **params)
    second = get(client, 'stock-dividends', **params, cursor=first['meta']['next_cursor'])
    assert first['data'][0]['selected_implementation'] and second['data'][0]['selected_implementation']
    assert second['meta']['next_cursor'] is None
    assert client.get(f'{BASE}/stock-dividends', params=params | dict(as_of='2026-11-01',
        cursor=first['meta']['next_cursor'])).status_code == 400
    raw.content_hash = 'invalid'
    session.commit()
    invalid = get(client, 'stock-dividends', **params)
    assert invalid['data'] == [] and invalid['meta']['evidence_status'] == 'invalid'


def test_undated_cancellation_blocks_implementation_only_once_known(saved, client):
    _, _, _, _, raw, put = saved
    put([row(), row(ann_date=None, div_proc='停止实施', imp_ann_date='20261101'),
         row(end_date='20241231', ann_date='20260502')])
    missing_id = f'raw.source_payload:{raw.id}:row:2'
    params = WINDOW | dict(start_date='2026-05-01', end_date='2026-05-01')
    captured = get(client, 'stock-dividends', **params, as_of='2026-10-31')
    assert captured['data'][0]['selected_implementation']
    assert captured['meta']['missing_announcement_rows'] == 0
    assert not captured['meta']['issues']
    for cutoff in ({'as_of':'2026-11-01'}, {}):
        page = get(client, 'stock-dividends', **params, **cutoff)
        implementation, = page['data']
        assert not implementation['selected_implementation']
        assert page['meta']['missing_announcement_rows'] == 1
        issue, = [i for i in page['meta']['issues'] if i['code'] == 'unlocated_cancellation']
        assert issue['sourceIds'] == [missing_id]
        assert issue['affectedSourceIds'] == [implementation['source_id']]
    previous_year = get(client, 'stock-dividends', **(params | dict(
        start_date='2026-05-02', end_date='2026-05-02', as_of='2026-11-01')))
    assert previous_year['data'][0]['selected_implementation'] and not previous_year['meta']['issues']


def test_undated_implementation_blocks_only_its_related_group(saved, client):
    *_, put = saved
    put([row(), row(ann_date=None), row(ann_date='20260502',
        record_date='20260610', ex_date='20260611', pay_date='20260612', div_listdate=None)])
    result = get(client, 'stock-dividends', **WINDOW, as_of='2026-10-31')
    first, second = result['data']
    assert not first['selected_implementation'] and second['selected_implementation']
    issue, = [i for i in result['meta']['issues'] if i['code'] == 'missing_ann_date']
    assert issue['affectedSourceIds'] == [first['source_id']]


def test_undated_cancellation_is_explained_on_page_with_only_core_mirror(saved, client):
    session, _, _, _, _, put = saved
    put([row(), row(ann_date=None, div_proc='停止实施')])
    session.add(Dividend(ts_code=SYMBOL, end_date=date(2025,12,31), ann_date=DAY, div_proc='实施',
                         cash_div_tax=Decimal('0.5'), record_date=date(2026,5,10)))
    session.commit()
    page = get(client, 'stock-dividends', **WINDOW, limit=1)
    implementation, = page['data']
    assert implementation['source_kind'] == 'core'
    issue, = [i for i in page['meta']['issues'] if i['code'] == 'unlocated_cancellation']
    assert implementation['source_id'] in issue['affectedSourceIds']


def test_reference_pages_and_card_date_alignment(client, pg_factory):
    other = '430001.BJ'
    with pg_factory.begin() as s:
        s.add_all([Security(ts_code=code, symbol=code[:6], name=code) for code in (SYMBOL, other)])
        s.flush()
        cat = Classification(provider='index', source='test', classification_id='A', name='类别')
        s.add(cat)
        s.flush()
        s.add_all([SecurityNameHistory(ts_code=SYMBOL, name='曾用名'),
            ClassificationMember(ts_code=SYMBOL, classification_id=cat.id, effective_from=DAY, effective_to=DAY),
            TradeCalendar(exchange='SSE', cal_date=DAY, is_open=False),
            IncomeStatement(ts_code=SYMBOL, ann_date=DAY, end_date=date(2025,12,31),
                report_type='1', data={'n_income_attr_p': 0}),
            DailyPrice(ts_code=SYMBOL, trade_date=DAY, close=0),
            DailyBasic(ts_code=SYMBOL, trade_date=date(2026,4,30), pb=2),
            AdjustmentFactor(ts_code=other, trade_date=DAY, adj_factor=2)])
    with pg_factory() as s:
        assert load_metric_card_inputs(s, (), DAY) == {}
        cards = load_metric_card_inputs(s, [SYMBOL, other], DAY, dividend_years={SYMBOL:{},other:{}})
        assert Decimal(cards[SYMBOL]['quote']['close']) == 0 and cards[SYMBOL]['quote']['pb'] is None
        assert cards[other]['quote']['tradeDate'] is None
    head = get(client, 'stock-securities', limit=1)
    assert head['data'][0]['symbol'] == other
    last = get(client, 'stock-securities', cursor=head['meta']['next_cursor'])
    assert last['data'][0]['names'][0]['name'] == '曾用名'
    assert client.get(f'{BASE}/stock-securities', params={'symbol':other,'cursor':head['meta']['next_cursor']}).status_code == 400
    days = get(client, 'stock-trade-calendar', exchange='SSE', start_date=DAY, end_date=DAY)
    assert days['data'][0]['is_open'] is False
    members = get(client, 'stock-classification-members', symbol=SYMBOL, start_date=DAY, end_date=DAY)
    assert members['data'][0]['classification_id'] == 'A'
    reports = get(client,'stock-financial-reports', symbol=SYMBOL, statement='income',
                  start_date='2025-01-01', end_date='2025-12-31')
    assert reports['data'][0]['ann_date'] == str(DAY) and reports['data'][0]['values']['n_income_attr_p'] == '0'


def test_disclosure_plan_and_announcement_based_actions(pg_factory, client):
    from app.db.models import DisclosureSchedule, FinancialForecast, Repurchase
    with pg_factory.begin() as s:
        s.add(Security(ts_code=SYMBOL, symbol=SYMBOL[:6], name='样本'))
        s.flush()
        s.add_all([DisclosureSchedule(ts_code=SYMBOL, end_date=DAY, pre_date=DAY),
            Repurchase(ts_code=SYMBOL, ann_date=DAY, proc='实施', data={'amount':0}),
            FinancialForecast(ts_code=SYMBOL, ann_date=DAY, end_date=date(2025,12,31),
                data={'type':'预增','net_profit_min':'0.123456789123456789'})])
    window = dict(symbol=SYMBOL, start_date=DAY, end_date=DAY)
    disclosure, = get(client, 'stock-disclosures', **window)['data']
    assert disclosure['actual_date'] is None and disclosure['pre_date'] == str(DAY)
    action, = get(client, 'stock-company-events', **window, kind='repurchase')['data']
    assert action['stage'] == '实施' and action['values']['amount'] == '0'
    event, = get(client, 'stock-company-events', **window, kind='forecast')['data']
    assert event['end_date']=='2025-12-31' and event['values']['net_profit_min']=='0.123456789123456789'


def test_portfolio_build_reads_same_stock_facts_as_public_datasets(new_pg_factory):
    from unittest.mock import patch
    from app.portfolio.marts import portfolio_runtime
    from app.stocks import dataset_query
    from app.stocks.derived import inputs
    from backend.tests.test_portfolio_rebuild import source

    f = source(new_pg_factory)
    with patch.object(portfolio_runtime, 'daily_rows_query', wraps=dataset_query.daily_rows_query) as daily, \
            patch.object(inputs, 'latest_price_with_basic', wraps=dataset_query.latest_price_with_basic) as quote:
        report = portfolio_runtime.rebuild_portfolio(f)
    assert report['status'] == 'planned'
    assert [(call.args[0], call.args[3]) for call in daily.call_args_list] == [
        (DailyPrice, ('600519.SH',)), (AdjustmentFactor, ('600519.SH',))]
    assert any(call.args == (('600519.SH',), date(2026,8,24)) for call in quote.call_args_list)
