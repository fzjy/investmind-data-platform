from datetime import date
from types import SimpleNamespace as Row
import pytest

def module():
    from app.stocks import shareholder_supply
    return shareholder_supply


def paid_entry(**values):
    return dict(basis='paid-cash-records-v1', querySource='raw.source_payload:1',
                recordedThrough='2024-08-01') | values

def test_concentration_uses_same_day_market_cap_and_disclosure_cutoff():
    rows=[Row(end_date=date(2025,12,31),ann_date=date(2026,3,1),holder_num=100,data={}),Row(end_date=date(2026,3,31),ann_date=date(2026,5,1),holder_num=50,data={})]
    basics=[Row(trade_date=date(2025,12,31),total_mv=100,data={})]
    result=module().concentration(rows,basics,date(2026,4,1))
    assert result[0]['points']==[{'period':'2025-12-31','value':100}]
    assert result[1]['points']==[{'period':'2025-12-31','value':10000}]
    assert module().concentration(rows,[],date(2026,4,1))[1]['points']==[]

def test_five_year_rank_requires_full_calendar_coverage_and_positive_values():
    asof=date(2026,10,21)
    rows=[Row(trade_date=date(2023,10,21),pe=10,pb=1),Row(trade_date=asof,pe=20,pb=2)]
    assert module().valuation_percentiles(rows,asof)=={'pePercentile':None,'pbPercentile':None}
    # 无完整交易日覆盖证明，即便首尾超过五年也不能冒充完整窗口。
    rows.insert(0,Row(trade_date=date(2021,10,21),pe=5,pb=.5))
    assert module().valuation_percentiles(rows,asof)['pePercentile'] is None


def test_verified_calendar_window_can_unlock_rank_without_using_three_year_metric():
    rows=[Row(trade_date=date(2021,10,21),pe=10,pb=1),Row(trade_date=date(2026,10,21),pe=20,pb=2)]
    result=module().valuation_percentiles(rows,date(2026,10,21),expected_days=[r.trade_date for r in rows],calendar_complete=True)
    assert result=={'pePercentile':100,'pbPercentile':100}


def test_missing_window_values_do_not_shrink_five_year_denominator():
    rows=[Row(trade_date=date(2021,10,21),pe=None,pb=None),Row(trade_date=date(2026,10,21),pe=20,pb=2)]
    result=module().valuation_percentiles(rows,date(2026,10,21),expected_days=[r.trade_date for r in rows],calendar_complete=True)
    assert result=={'pePercentile':None,'pbPercentile':None}


def test_verified_raw_window_retains_conflicts_and_core_only_extends_later_dates():
    history=[Row(trade_date=date(2026,10,20),sourceAsOf=date(2026,10,21),pe=None,pb=1)]
    core=[Row(trade_date=date(2026,10,20),pe=20,pb=None),Row(trade_date=date(2026,10,21),pe=30,pb=2),Row(trade_date=date(2026,10,22),pe=31,pb=3)]
    result=module().merge_valuation_rows(history,core)
    assert [r.trade_date for r in result]==[date(2026,10,20),date(2026,10,22)]
    assert result[0].pe is None and result[0].pb==1

def test_observed_dividends_are_not_verified_complete_fy_or_three_year_mean():
    result=module().annual_returns([{'fiscalYear':'2025','cashPerShare':2,'cashAmount':100,'sourceIds':['raw:1']}],{},date(2026,10,21),price=10)
    assert result['annualReturns'][0]['observedDps']==2
    assert result['researchValues']['dividendYield'] is None
    assert result['researchValues']['payoutRatio'] is None
    assert result['availability']['dividendSeries']['status']=='partial'

def test_repurchase_without_program_identity_never_sums_repeated_progress_announcements():
    rows=[Row(ann_date=date(2025,6,d),proc='完成',data={'amount':100}) for d in (1,2)]
    result=module().repurchase_evidence(rows,date(2026,1,1))
    assert result['complete'] is False and result['annual']==[] and result['observedRecords']==2


def test_complete_future_fiscal_contract_can_unlock_fy_and_three_year_mean():
    events=[{'fiscalYear':str(y),'cashPerShare':2,'cashAmount':20,'stockRatio':0,'baseShares':10,'sourceIds':[]} for y in (2023,2024,2025)]
    result=module().annual_returns(events,{str(y):100 for y in (2023,2024,2025)},date(2026,10,21),price=10,history_complete=True)
    assert result['researchValues']['dividendYield']==20
    assert result['researchValues']['payoutRatio']==20


def test_unknown_capital_does_not_make_multiple_events_same_per_share_basis():
    result=module().annual_returns([{'fiscalYear':'2025','cashPerShare':1},{'fiscalYear':'2025','cashPerShare':2}],{},date(2026,10,21))
    assert result['annualReturns'][0]['observedDps'] is None


def test_company_ledger_survives_research_only_base_change_and_rejects_future_day():
    from sqlalchemy import select
    from app.db.models import PublishedSnapshot
    from backend.tests.test_portfolio_routine import source,ObservedClient,refresh
    f=source();refresh(f,ObservedClient(f))
    before=module().load_company_projections(f,('600519.SH',),date(2026,8,24))
    with f.begin() as s:s.get(PublishedSnapshot,'screener').version='research-only-change'
    after=module().load_company_projections(f,('600519.SH',),date(2026,8,24))
    assert after==before
    assert next(r for r in after['600519.SH']['annualReturns'] if r['year']=='2025')['observedDps']==1
    future=module().load_company_projections(f,('600519.SH',),date(2026,8,25))
    assert not any(r['complete'] for r in future['600519.SH']['annualReturns'])


def test_annual_profits_never_pick_arbitrary_equal_version_conflict():
    from app.stocks.derived.inputs import FinancialPoint
    points=[FinancialPoint(date(2025,12,31),date(2026,4,1),'1',{'n_income_attr_p':v,'update_flag':'1'},'600519.SH','core.income_statement',str(v)) for v in (100,200)]
    assert module().annual_profits(points,date(2026,10,21))=={'2025':None}
    points.append(FinancialPoint(date(2025,12,31),date(2026,5,1),'1',{'n_income_attr_p':300},'600519.SH','core.income_statement','latest'))
    assert module().annual_profits(points,date(2026,10,21))=={'2025':300}

def test_shareholder_annual_distribution_cash_basis():
    from app.stocks.shareholder_returns import annual_distributions
    from datetime import date
    events = [dict(status='paid', cashPerShare=dps, baseShare=shares, baseDate='2023-01-01', recordDate='2023-05-01', sourceIds=[source]) for dps, shares, source in [('0.30', '10000', 'a'), ('0.20', '20000', 'b')]]
    row = annual_distributions({'2023': paid_entry(covered=True, events=events)}, {'2023': '50000000'}, date(2024, 8, 1))[0]
    assert row['dps'] == .5
    assert row['cashAmount'] == 70000000
    assert row['payoutRatio'] == 140
    events[1]['baseShare'] = None
    row = annual_distributions({'2023': paid_entry(covered=True, events=events)}, {'2023': '50000000'}, date(2024, 8, 1))[0]
    assert row['dps'] == .5 and row['cashAmount'] is None and row['payoutRatio'] is None
    assert row['observedCashAmount'] == 30000000
    assert row['asOf'] == '2024-08-01'
    assert any('b' in reason for reason in row['missing'])

def test_shareholder_concentration_same_day_units():
    from app.stocks.shareholder_returns import concentration_periods
    from datetime import date
    from types import SimpleNamespace
    period = date(2023, 12, 31)
    count = SimpleNamespace(ann_date=date(2024, 1, 2), end_date=period, holder_num=2000)
    basic = SimpleNamespace(trade_date=period, total_mv=10000)
    row = concentration_periods([count], [basic], date(2024, 2, 1))[0]
    assert row['marketCap'] == 100000000 and row['averageAmount'] == 50000
    row = concentration_periods([count], [], date(2024, 2, 1))[0]
    assert row['holderCount'] == 2000 and row['averageAmount'] is None

def test_shareholder_annual_output_states():
    from app.stocks.shareholder_returns import annual_distributions
    from datetime import date
    event = dict(status='paid', cashPerShare='0.3', baseShare='10000', baseDate='2023-01-01', recordDate='2023-05-01', sourceIds=['verified'])
    def calculate(entry, profit='50000000'):
        return annual_distributions({'2023': paid_entry(**entry)}, {'2023': profit}, date(2024, 8, 1))[0]
    row = calculate(dict(covered=False, events=[event]))
    assert row['dps'] is None and row['observedDps'] is not None and row['payoutRatio'] is None
    row = calculate(dict(covered=False, issues=['paid_revision_conflict'], events=[event]))
    assert row['status'] == 'partial' and row['cashAmount'] is None
    row = calculate(dict(covered=True, events=[event | dict(status='pending')]))
    assert row['payoutRatio'] == row['dps'] == 0
    row = calculate(dict(covered=True, zeroConfirmed=True, events=[]))
    assert row['dps'] == 0 and row['cashAmount'] == 0 and row['payoutRatio'] == 0
    row = calculate(dict(covered=True, events=[event]), '-1')
    assert row['parentProfit'] == -1 and row['payoutRatio'] is None
    row = calculate(dict(covered=True, events=[event | dict(baseDate=None, sourceIds=[])]))
    assert row['dps'] is not None and row['cashAmount'] is None

def test_shareholder_paid_basis_revision_conflict():
    from app.stocks.shareholder_returns import annual_distributions, paid_basis_conflict
    from datetime import date
    event = dict(period='2023-12-31', status='paid', payDate='2024-06-01', cashPerShare='0.3', baseShare='20000', baseDate='2024-05-01', recordDate='2024-05-30', sourceIds=['new'])
    old = dict(end_date='2023-12-31', div_proc='实施', pay_date='2024-06-01', cash_div_tax='0.3', base_share='10000', base_date='2024-05-01', record_date='2024-05-30', imp_ann_date='2024-05-15')
    new = old | dict(base_share='20000', imp_ann_date='2024-06-10')
    row = annual_distributions({'2023': paid_entry(covered=True, events=[event], observations=[old, new])}, {'2023': 50000000}, date(2024, 8, 1))[0]
    assert row['dps'] is not None and row['cashAmount'] is None and row['payoutRatio'] is None
    assert not paid_basis_conflict(event, [old | dict(base_share=None), new])
    assert not paid_basis_conflict(event, [old, new | dict(record_date='2024-05-31')])
    assert not paid_basis_conflict(event, [old, new | dict(imp_ann_date='2024-05-20')])
    assert not paid_basis_conflict(event, [old, new | dict(div_proc='预案')])

def test_shareholder_paid_basis_revision_stage_dates_and_identity():
    from app.stocks.shareholder_returns import paid_basis_conflict
    event = dict(period='2023-12-31', status='paid', payDate='2024-05-03', cashPerShare='0.3', recordDate='2024-05-01', exDate='2024-05-02')
    old = dict(end_date='2023-12-31', div_proc='实施完成', ann_date='2024-04-01', imp_ann_date='2024-04-10', record_date='2024-05-01', ex_date='2024-05-02', pay_date='2024-05-03', cash_div_tax='0.3', base_share='10000', base_date='2024-04-01')
    new = old | dict(ann_date='2024-06-01', base_share='20000')
    assert paid_basis_conflict(event, [old, new])
    assert paid_basis_conflict(event, [old, new | dict(ex_date='2024-05-03')])
    assert paid_basis_conflict(event, [old, new, new | dict(ann_date='2024-07-01', base_share=None, base_date=None)])
    assert not paid_basis_conflict(event, [old, new | dict(ann_date='2024-04-15')])
