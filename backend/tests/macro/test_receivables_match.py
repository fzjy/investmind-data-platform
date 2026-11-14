from dataclasses import replace
import importlib
import importlib.util

import pytest

from app.connectors.tushare.contracts import TushareResponse
from backend.tests.macro.receivables_samples import alter_response,request_and_source,response_for


def match(request,source,response,limit=5000):
    if importlib.util.find_spec('app.macro.receivables_match') is None:
        pytest.fail('receivables witness matcher not implemented')
    return importlib.import_module('app.macro.receivables_match').match_receivable(request,source,response,limit)


@pytest.mark.parametrize('value,status,expected',[(0,'matched','0'),('123.450','matched','123.450'),
    (None,'returned-null',None),(True,'invalid-value',None),('NaN','invalid-value',None),
    ('Infinity','invalid-value',None),('not-money','invalid-value',None)])
def test_only_finite_values_are_usable_and_zero_is_not_missing(value,status,expected):
    request,source=request_and_source()
    assert match(request,source,response_for(source,value))=={'status':status,'value':expected}


def test_equal_decimal_formats_and_null_witness_are_not_missing_columns():
    request,source=request_and_source()
    response=alter_response(response_for(source),change={'money_cap':'100.000','goodwill':None})
    assert match(request,source,response)=={'status':'matched','value':'123'}


@pytest.mark.parametrize('condition,status',[
    ('source-missing','source-witness-missing'),('response-missing','response-witness-missing'),
    ('target-missing','missing-field'),('no-match','no-match'),('empty','no-match'),
    ('duplicate','ambiguous'),('out-of-scope','identity-mismatch'),('at-limit','truncated')])
def test_non_bindable_response_is_explicit_not_zero(condition,status):
    request,source=request_and_source(); response=response_for(source)
    limit=5000
    if condition=='source-missing':
        values={key:value for key,value in source.values.items() if key!='money_cap'}
        # 请求也对应这个缺见证源版本，不能靠绑定不符提前通过该分支。
        data=importlib.import_module('backend.tests.macro.receivables_samples').full_inputs()
        data=replace(data,financials=tuple(replace(r,values=values) if r.source_key==source.source_key else r for r in data.financials))
        request,source=request_and_source(data)
    elif condition=='response-missing': response=alter_response(response,remove=['money_cap'])
    elif condition=='target-missing': response=alter_response(response,remove=['accounts_receiv'])
    elif condition=='no-match': response=alter_response(response,change={'inventories':999})
    elif condition=='empty': response=response.model_copy(update={'items':()})
    elif condition=='duplicate': response=response.model_copy(update={'items':(*response.items,*response.items)})
    elif condition=='out-of-scope': response=alter_response(response,change={'ts_code':'600999.SH'})
    else: limit=1
    assert match(request,source,response,limit)=={'status':status,'value':None}


def test_changed_source_cannot_be_bound_to_previous_request():
    request,source=request_and_source()
    changed=replace(source,values={**source.values,'money_cap':999})
    with pytest.raises(ValueError,match='SOURCE_BINDING'):
        match(request,changed,response_for(changed))


def test_duplicate_response_columns_are_rejected_not_silently_collapsed():
    request,source=request_and_source(); response=response_for(source)
    duplicate=TushareResponse(fields=(*response.fields,'accounts_receiv'),items=((*response.items[0],999),))
    with pytest.raises(ValueError,match='RESPONSE_INVALID'):
        match(request,source,duplicate)
