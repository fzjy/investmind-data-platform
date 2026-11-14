from copy import deepcopy
from datetime import date
import pytest

AS_OF = date(2026, 10, 21)

def table(kind, periods, values, **overrides):
    return dict(kind=kind, periods=periods, amountUnit='yuan',
        reportMetadata=[dict(period=p, announcementDate=f'{int(p[:4])+1}-03-01' if p.endswith('12-31') else p[:4]+'-10-01', reportType='1') for p in periods],
        rows=[dict(label=k, values=v) for k,v in values.items()]) | overrides

def build(income=None, cash=None):
    from app.stocks.operating_history import operating_history
    return operating_history([t for t in (income,cash) if t is not None], AS_OF)

def sample(periods=None):
    periods = periods or ['2025-03-31','2025-06-30','2025-09-30','2025-12-31']
    return table('income',periods,dict(revenue=[100,220,360,500],n_income_attr_p=[10,25,35,50])), table('cashflow',periods,dict(n_cashflow_act=[20,40,80,110],c_pay_acq_const_fiolta=[5,12,20,30]))

def test_annual_and_true_quarter_values_use_standard_legs_and_units():
    a,b=sample(); data=build(a,b)
    assert data['amountUnit']=='yuan' and data['historyComplete'] is False
    assert data['asOf']=='2026-10-21'
    annual=data['annual'][0]['values']; quarters=data['quarterly']
    assert annual['revenue']==500 and annual['fcf']==80
    assert [r['values']['revenue'] for r in quarters]==[100,120,140,140]
    assert [r['values']['fcf'] for r in quarters]==[15,13,32,20]
    assert annual['netMargin']==10 and annual['realMoneyIndex']==2.2 and annual['capexRate']==6
    assert quarters[-1]['values']['netMargin']==pytest.approx(15/140*100)

def test_exact_previous_quarter_missing_is_unknown_never_jump_or_cross_year():
    a,b=sample(); a['periods'][1]='2024-06-30'; b['periods'][1]='2024-06-30'
    for t in (a,b):
        t['reportMetadata'][1]['period']='2024-06-30';t['reportMetadata'][1]['announcementDate']='2024-08-01'
    q={r['period']:r for r in build(a,b)['quarterly']}
    assert q['2025-09-30']['values']['revenue'] is None
    assert '前季' in q['2025-09-30']['reasons']['revenue']
    assert q['2024-06-30']['values']['fcf'] is None

def test_missing_field_does_not_borrow_other_scope_and_capex_is_not_investing_flow():
    a,b=sample();a['rows'][0]['values'][-1]=None
    a['rows'].append(dict(label='total_revenue',values=[1000]*4))
    b['rows']=[r for r in b['rows'] if r['label']!='c_pay_acq_const_fiolta']
    b['rows'].append(dict(label='n_cashflow_inv_act',values=[-100]*4))
    annual=build(a,b)['annual'][0]['values']
    assert annual['revenue'] is None and annual['capex'] is None and annual['fcf'] is None

def test_revenue_alias_only_when_canonical_row_absent_and_duplicate_conflict_local():
    a,b=sample();a['rows'][0]['label']='total_revenue'
    assert build(a,b)['annual'][0]['values']['revenue']==500
    a['rows'].append(dict(label='total_revenue',values=[100,220,360,501]))
    v=build(a,b)['annual'][0]['values']
    assert v['revenue'] is None and v['profit']==50
    assert build(a,b)==build(deepcopy(a),deepcopy(b))

@pytest.mark.parametrize('bad',[None,False,float('nan'),float('inf'),'bad'])
def test_nonfinite_and_unknown_income_preserved_without_erasing_cash(bad):
    a,b=sample();a['rows'][1]['values'][-1]=bad
    annual=build(a,b)['annual'][0]
    assert annual['values']['profit'] is None and annual['values']['realMoneyIndex'] is None
    assert annual['values']['fcf']==80 and annual['reasons']['profit']

@pytest.mark.parametrize('field,value', [('revenue',0),('revenue',-50),('n_income_attr_p',0),('n_income_attr_p',-2)])
def test_nonpositive_denominators_not_misrepresented_as_useful_ratio(field,value):
    a,b=sample(); next(r for r in a['rows'] if r['label']==field)['values'][-1]=value
    result=build(a,b)['annual'][0]
    key='netMargin' if field=='revenue' else 'realMoneyIndex'
    assert result['values'][key] is None and result['reasons'][key]

def test_negative_capex_and_negative_quarter_capex_remain_raw_but_fcf_unknown():
    a,b=sample();b['rows'][1]['values'][-1]=-1
    r=build(a,b)['annual'][0]
    assert r['values']['capex']==-1 and r['values']['fcf'] is None and r['values']['capexRate'] is None
    b['rows'][1]['values'][-1]=19
    assert build(a,b)['quarterly'][-1]['values']['fcf'] is None

def test_cagr_is_exact_three_year_endpoints_not_three_reports_or_quarter_growth():
    periods=['2020-12-31','2022-12-31','2023-12-31','2025-12-31']
    a=table('income',periods,dict(revenue=[100,100,133.1,172.8],n_income_attr_p=[-2,10,4,20]))
    rows={r['period']:r['values'] for r in build(a)['annual']}
    assert rows['2023-12-31']['revenueCagr3']==pytest.approx(10)
    assert rows['2023-12-31']['profitCagr3'] is None
    assert rows['2025-12-31']['revenueCagr3']==pytest.approx(20)
    assert rows['2022-12-31']['revenueCagr3'] is None
    assert all(r['values']['revenueCagr3'] is None for r in build(a)['quarterly'])

@pytest.mark.parametrize('change', ['missing','future','invalid','parent','unit','duplicate'])
def test_metadata_unit_or_ambiguous_table_cannot_feed_calculations(change):
    from app.stocks.operating_history import operating_history
    a,b=sample()
    if change=='missing':a.pop('reportMetadata')
    elif change=='future':a['reportMetadata'][-1]['announcementDate']='2026-12-01'
    elif change=='invalid':a['reportMetadata'][-1]['announcementDate']='2024-01-01'
    elif change=='parent':a['reportMetadata'][-1]['reportType']='4'
    elif change=='unit':a['amountUnit']='unknown'
    result=operating_history([a,b,*([deepcopy(a)] if change=='duplicate' else [])],AS_OF)['annual'][0]
    assert result['values']['revenue'] is None
    assert result['values']['ocf']==110
    assert result['reasons']['revenue']

def test_empty_and_future_or_nonquarter_periods_do_not_create_facts():
    assert build()['annual']==[] and build()['quarterly']==[]
    a=table('income',['2026-12-31','2025-05-31'],dict(revenue=[100,100]))
    assert build(a)['quarterly']==[]

def test_overflow_and_underflow_are_unknown_not_infinity_or_false_zero():
    a,b=sample();a['rows'][0]['values'][-1]='1e-400';b['rows'][0]['values'][-1]='1e400'
    r=build(a,b)['annual'][0]['values']
    assert r['revenue'] is None and r['ocf'] is None and r['fcf'] is None

def test_input_is_not_modified_and_source_announcement_is_real():
    a,b=sample();before=deepcopy((a,b));r=build(a,b)
    assert (a,b)==before
    assert r['annual'][0]['announcedAt']=='2026-03-01'
