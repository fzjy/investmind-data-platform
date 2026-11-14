from copy import deepcopy
from dataclasses import replace
import importlib
import importlib.util

import pytest

from app.connectors.tushare.contracts import TushareResponse
from backend.tests.macro.test_rea_input_preview import data, preview


def module():
    assert importlib.util.find_spec('app.macro.rea_input_match'), 'REA input matcher not implemented'
    return importlib.import_module('app.macro.rea_input_match')


def sample(table='balance'):
    original=data()
    request=next(r for r in preview(original)['requests'] if r['table']==table)
    source=next(r for r in original.financials if r.source_key==request['coreBinding']['sourceKey'])
    values={**source.values,'ts_code':source.symbol,'end_date':source.end_date.strftime('%Y%m%d'),
        'ann_date':source.ann_date.strftime('%Y%m%d'),'f_ann_date':None,
        'report_type':source.report_type,'comp_type':source.comp_type,'update_flag':source.update_flag,
        **{field:7 for field in request['missingFields']}}
    return request,source,TushareResponse(fields=request['fields'],items=[[values[field] for field in request['fields']]])


def change(response,field,value):
    rows=[list(row) for row in response.items]
    rows[0][response.fields.index(field)]=value
    return TushareResponse(fields=response.fields,items=rows)


@pytest.mark.parametrize('table',['balance','cashflow'])
def test_unique_full_witness_match_returns_only_absent_fields_without_mutation(table):
    request,source,response=sample(table)
    before=(deepcopy(request),deepcopy(source.values),response.model_dump())
    result=module().match_rea_inputs(request,source,response,5000)
    assert result['status']=='matched'
    assert result['fields']=={field:{'status':'matched','value':'7'} for field in request['missingFields']}
    assert (request,source.values,response.model_dump())==before


@pytest.mark.parametrize('value,status,public',[(0,'matched','0'),(None,'returned-null',None),
    (-1,'invalid-value',None),(True,'invalid-value',None),('NaN','invalid-value',None),('Infinity','invalid-value',None)])
def test_target_fields_are_independent_and_missing_is_not_zero(value,status,public):
    request,source,response=sample()
    result=module().match_rea_inputs(request,source,change(response,'fix_assets',value),5000)
    assert result['status']=='matched'
    assert result['fields']['fix_assets']=={'status':status,'value':public}
    assert result['fields']['acct_payable']=={'status':'matched','value':'7'}


@pytest.mark.parametrize('kind,expected',[('identity','identity-mismatch'),('witness','no-match'),
    ('ambiguous','ambiguous'),('empty','no-match'),('missing-witness','response-witness-missing'),('truncated','truncated')])
def test_response_must_bind_unique_old_core_before_any_field_can_be_used(kind,expected):
    request,source,response=sample()
    limit=5000
    if kind=='identity': response=change(response,'ts_code','999999.SH')
    elif kind=='witness': response=change(response,'money_cap',999)
    elif kind=='ambiguous': response=TushareResponse(fields=response.fields,items=response.items*2)
    elif kind=='empty': response=TushareResponse(fields=response.fields,items=[])
    elif kind=='missing-witness':
        index=response.fields.index('money_cap')
        response=TushareResponse(fields=[f for f in response.fields if f!='money_cap'],items=[[v for i,v in enumerate(response.items[0]) if i!=index]])
    else: limit=1
    result=module().match_rea_inputs(request,source,response,limit)
    assert result=={'status':expected,'fields':{}}


def test_missing_one_target_does_not_hide_the_other_valid_target():
    request,source,response=sample()
    index=response.fields.index('fix_assets')
    response=TushareResponse(fields=[f for f in response.fields if f!='fix_assets'],items=[[v for i,v in enumerate(response.items[0]) if i!=index]])
    result=module().match_rea_inputs(request,source,response,5000)
    assert result['fields']=={'fix_assets':{'status':'missing-field','value':None},'acct_payable':{'status':'matched','value':'7'}}


@pytest.mark.parametrize('kind',['source','request','fields','duplicate-columns','bad-row','bad-limit'])
def test_tampered_protocol_is_rejected_not_bound(kind):
    request,source,response=sample()
    limit=5000
    if kind=='source': source=replace(source,values={**source.values,'money_cap':999})
    elif kind=='request': request={**request,'requestKey':'0'*64}
    elif kind=='fields': request={**request,'fields':[*request['fields'],'arbitrary_field']}
    elif kind=='duplicate-columns': response=TushareResponse(fields=[*response.fields,'money_cap'],items=[[*response.items[0],40]])
    elif kind=='bad-row': response=TushareResponse(fields=response.fields,items=[response.items[0][:-1]])
    else: limit=True
    with pytest.raises(ValueError): module().match_rea_inputs(request,source,response,limit)
