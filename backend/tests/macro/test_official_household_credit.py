from importlib import import_module

import pytest


def document(amount='1.21',unit='万亿元',direction='增加',title='2026年前三季度金融统计数据报告',currency='人民币'):
    scope=title.replace('金融统计数据报告','')
    return f'<html><meta charset="utf-8"><h1>{title}</h1><h2>二、{scope}{currency}贷款增加16.02万亿元</h2><p>分部门看，住户贷款增加2.1万亿元，其中，短期贷款减少300亿元，中长期贷款{direction}{amount}{unit}；企（事）业单位贷款增加12万亿元，其中，中长期贷款增加9万亿元。</p><h2>三、人民币存款增加20万亿元</h2></html>'.encode()


def module():return import_module('app.macro.official_macro_normalize')
def parse(body=None,period='2026-09'):
    return module().parse_household_ytd(document() if body is None else body,period)


def test_only_household_rmb_medium_long_term_ytd_is_normalized():
    value=parse();point=value['points'][0]
    assert value['role']=='household-ytd' and value['source']=='pboc' and value['period']=='2026-09-01'
    assert len(value['rawSha256'])==64 and len(value['points'])==1
    assert point['seriesId']=='cn.credit.household_medium_long_term_ytd'
    assert point['value']=='12100' and point['unit']=='亿元' and point['frequency']=='monthly'


@pytest.mark.parametrize('amount,unit,direction,expected',[
    ('1.21','万亿元','增加','12100'),('3,291','亿元','增加','3291'),
    ('0','亿元','减少','0'),('12.50','亿元','减少','-12.5'),('0.0001','万亿元','增加','1'),
])
def test_explicit_units_signed_net_decreases_and_real_zero(amount,unit,direction,expected):
    assert parse(document(amount,unit,direction))['points'][0]['value']==expected


@pytest.mark.parametrize('title,period',[
    ('2026年前八个月金融统计数据报告','2026-08'),('2026年前8个月金融统计数据报告','2026-08'),
    ('2026年上半年金融统计数据报告','2026-06'),('2026年一季度金融统计数据报告','2026-03'),
    ('2026年1月份金融统计数据报告','2026-01'),('2026年金融统计数据报告','2026-12'),
])
def test_report_title_proves_declared_year_and_ytd_period(title,period):
    assert parse(document(title=title),period)['period']==period+'-01'


@pytest.mark.parametrize('body',[
    document(title='2025年前三季度金融统计数据报告'),document(title='2026年前八个月金融统计数据报告'),
    document(currency='外币'),document(unit='万户'),document(amount='NaN'),document(amount='1e6'),
    document(amount='-1'),document(amount='1,21'),document(amount='1<br>2'),
    document().replace('中长期贷款增加1.21'.encode(),'中长期贷款同比少增1.21'.encode()),
    document().replace('住户贷款'.encode(),'企业贷款'.encode()),
    document().replace('前三季度金融'.encode(),'前八个月金融'.encode()),
    b'<script>'+document()+b'</script>',b'',b'\xff',
    document()+document(amount='2'),
])
def test_other_year_currency_enterprise_loan_bad_amount_or_ambiguous_body_rejected(body):
    with pytest.raises(ValueError):parse(body)


@pytest.mark.parametrize('period',['2026-13','2026-02-30','2026-09-02','202609',None,True])
def test_month_identity_is_strict(period):
    with pytest.raises(ValueError):parse(period=period)


def test_household_months_use_only_adjacent_same_year_cumulative_and_january_is_direct():
    values={'2025-12-01':'300','2026-01-01':'10','2026-02-01':'8','2026-03-01':'8','2026-05-01':'15'}
    result=module().derive_household_months(values)
    assert result==[{'month':'2025-12-01','value':None},{'month':'2026-01-01','value':'10'},
        {'month':'2026-02-01','value':'-2'},{'month':'2026-03-01','value':'0'},{'month':'2026-05-01','value':None}]
    assert module().derive_household_months({})==[]


@pytest.mark.parametrize('values',[
    {'2026-02-30':'1'},{'2026-02-15':'1'},{'2026-02-01':True},
    {'2026-02-01':'NaN'},{'2026-02-01':'Infinity'},{'2026-02-01':'1e3'},
])
def test_bad_cumulative_map_is_not_silently_dropped(values):
    with pytest.raises(ValueError):module().derive_household_months(values)
