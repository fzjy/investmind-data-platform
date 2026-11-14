from datetime import date
from types import SimpleNamespace as Row
from app.stocks.workbench_enrichment import holder_periods, valuation_bands

def test_holders_respect_versions_and_preserve_actual_percentages():
    rows=[Row(end_date=date(2025,12,31),ann_date=date(2026,4,d),holder_name='机构A',data={'hold_ratio':r}) for d,r in [(1,20),(2,25)]]
    result=holder_periods(rows)
    assert result[0]['holders'][0]['ratio']==25
    assert result[0]['holders'][0]['cumulative']==25
    rows.append(Row(end_date=date(2025,12,31),ann_date=date(2026,4,2),holder_name='机构A',data={'hold_ratio':30}))
    assert holder_periods(rows)[0]['holders']==[]

def test_revised_report_does_not_resurrect_holder_removed_from_new_version():
    rows=[Row(end_date=date(2025,12,31),ann_date=date(2026,4,d),holder_name=name,data={'hold_ratio':20}) for d,name in [(1,'旧股东'),(2,'新股东')]]
    assert [h['name'] for h in holder_periods(rows)[0]['holders']]==['新股东']

def test_bands_use_actual_finite_positive_daily_multiples_not_fake_curves():
    rows=[Row(trade_date=date(2026,1,i),pe=p,pb=2,data={}) for i,p in [(1,-1),(2,10),(3,20)]]
    result=valuation_bands(rows)
    assert result[0]['points']==[{'period':'2026-01-02','value':10},{'period':'2026-01-03','value':20}]
    assert result[0]['current']==20 and len(result[0]['bands'])==5
    assert result[2]['points']==[]


def test_missing_ratio_uses_same_report_capital_in_shares_not_float_capital():
    rows=[Row(end_date=date(2025,12,31),ann_date=date(2026,4,2),holder_name='机构A',hold_amount=250000,data={})]
    basics=[Row(trade_date=date(2025,12,31),data={'total_share':100,'float_share':50})]
    h=holder_periods(rows,basics=basics)[0]['holders'][0]
    assert h['ratio']==25 and h['basis']=='报告期持股数量/总股本'
    assert holder_periods(rows,basics=[Row(trade_date=date(2026,1,1),data={'total_share':100})])[0]['holders']==[]
    assert holder_periods(rows,basics=[Row(trade_date=date(2025,12,1),data={'total_share':100})])[0]['holders']==[]


def test_derived_ratio_conflicts_and_true_zero_are_preserved():
    base=dict(end_date=date(2025,12,31),ann_date=date(2026,4,2),holder_name='A',data={})
    basics=[Row(trade_date=date(2025,12,31),data={'total_share':100})]
    assert holder_periods([Row(**base,hold_amount=0)],basics=basics)[0]['holders'][0]['ratio']==0
    assert holder_periods([Row(**base,hold_amount=v) for v in (0,1)],basics=basics)[0]['holders']==[]


def test_invalid_explicit_ratio_is_not_hidden_by_quantity_fallback():
    row=Row(end_date=date(2025,12,31),ann_date=date(2026,4,2),holder_name='A',hold_amount=100,data={'hold_ratio':True})
    basics=[Row(trade_date=date(2025,12,31),data={'total_share':100})]
    assert holder_periods([row],basics=basics)[0]['holders']==[]
