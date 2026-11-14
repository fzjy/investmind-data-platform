from copy import deepcopy
from importlib import import_module

import pytest

from backend.tests.macro.test_exchange_summary_plan import build


def business(p=None,value='123456789.1234567890'):
    p=p or build();doc=p['documents'][0]
    fields=doc['query']['fields'];params=doc['query']['params']
    return {'fields':fields,'items':[[params.get(f,value) for f in fields]]}


def normalize(p=None,data=None,doc=None):
    p=p or build();doc=doc or p['documents'][0]
    return import_module('app.macro.exchange_summary_normalize').normalize_exchange_response(p,doc,business(p) if data is None else data)


def test_exact_amount_and_named_exchange_daily_identity():
    p=build();r=normalize(p)
    assert r['status']=='observed' and r['source']=='tushare'
    point=r['points'][0]
    assert point['value']=='123456789.123456789' and point['unit']=='元'
    assert point['seriesId']=='cn.market.margin_balance.sse' and point['period']=='2026-10-22' and point['frequency']=='daily'
    q=build(group='aShareFloatMv');other=normalize(q)
    assert other['points'][0]['unit']=='亿元' and other['points'][0]['seriesId']=='cn.market.a_share_float_mv.sh'


def test_zero_null_and_successfully_empty_are_distinct_terminal_outcomes():
    p=build()
    assert normalize(p,business(p,0))['points'][0]['value']=='0'
    null=normalize(p,business(p,None));empty=normalize(p,{'fields':p['documents'][0]['query']['fields'],'items':[]})
    assert null['status']=='null' and empty['status']=='empty' and null['points']==empty['points']==[]


def test_columns_located_by_name_not_position():
    b=business();b['fields'].reverse();b['items'][0].reverse()
    assert normalize(data=b)==normalize()


@pytest.mark.parametrize('value',[True,False,-1,'-0.01','NaN','Infinity',float('nan'),'1e40','1e-11','1,000',' 2','',{},[]])
def test_invalid_or_unrepresentable_amount_is_not_rounded_or_coerced(value):
    with pytest.raises(ValueError):normalize(data=business(value=value))


@pytest.mark.parametrize('mutation',['date','exchange','extra-field','missing-field','duplicate-field','extra-row','wrong-row','extra-key','bad-doc','tampered-plan'])
def test_response_cannot_expand_target_date_security_scope_or_row_cap(mutation):
    p=build();b=business(p);doc=deepcopy(p['documents'][0])
    if mutation=='date':b['items'][0][0]='20261023'
    elif mutation=='exchange':b['items'][0][1]='SZSE'
    elif mutation=='extra-field':b['fields'].append('rzrqye');b['items'][0].append('1')
    elif mutation=='missing-field':b['fields'].pop();b['items'][0].pop()
    elif mutation=='duplicate-field':b['fields'][1]=b['fields'][0]
    elif mutation=='extra-row':b['items'].append(deepcopy(b['items'][0]))
    elif mutation=='wrong-row':b['items'][0]=[1]
    elif mutation=='extra-key':b['token']='test-secret'
    elif mutation=='bad-doc':doc['seriesId']='other'
    else:p['groupPolicy']['unit']='亿元'
    with pytest.raises(ValueError):normalize(p,b,doc)
