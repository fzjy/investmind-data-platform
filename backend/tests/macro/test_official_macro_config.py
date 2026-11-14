from copy import deepcopy
from importlib import import_module

import pytest


def document(key,role,period,url,derived=None):
    return {'id':key,'role':role,'period':period,'url':url,'derivedFrom':derived}


def policy(group='householdCredit'):
    documents={
        'householdCredit':[
            document('household-202608','household-ytd','2026-08','https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026091417020862747/index.html'),
            document('household-202609','household-ytd','2026-09','https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026101417073743604/index.html')],
        'tier1Housing':[document('housing-202609','tier1-prices','2026-09','https://www.stats.gov.cn/xxgk/jd/sjjd2020/202610/t20261019_1965610.html')],
        'specialBonds':[
            document('quota-2026','special-quota','2026','https://yss.mof.gov.cn/2026zyczys/202603/t20260324_3985998.htm'),
            document('special-202608','special-issuance','2026-08','https://zwgls.mof.gov.cn/tjsj/202609/t20260924_3998108.htm'),
            document('special-202609','special-briefing','2026-09','https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202610/t20261023_4003429.htm')],
        'propertyLoans':[
            document('property-directory','property-directory','latest','https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html'),
            document('property-report','property-report','latest-completed-quarter',None,'property-directory')],
    }
    cap={'householdCredit':2,'tier1Housing':1,'specialBonds':3,'propertyLoans':2}[group]
    return {'collectionEnabled':False,'schedule':{'frequency':'weekly','weekday':0,'time':'09:00','timezone':'Asia/Shanghai'},
        'lookbackPeriods':48,'quoteSide':None,'quarterFallbacks':1 if group=='propertyLoans' else 0,
        'limits':{'maxHttpPerRun':cap,'maxHttpPerPeriod':cap,'requestsPerMinute':10,'requestTimeoutSeconds':20,
            'maxRetries':0,'maxResponseBytes':5242880,'maxResponseRows':16,'maxDirectoryCandidates':2},
        'documents':documents[group]}


def load(group,value=None):
    return import_module('app.macro.extensions.config').validate_group(group,policy(group) if value is None else value)


@pytest.mark.parametrize('group',['householdCredit','tier1Housing','specialBonds','propertyLoans'])
def test_closed_official_macro_group_and_initial_budget(group):
    result=load(group)
    assert not result.collectionEnabled and result.lookbackPeriods==48
    assert result.limits.maxHttpPerPeriod==len(result.documents)
    assert result.schedule.timezone=='Asia/Shanghai'


@pytest.mark.parametrize('field,bad',[
    ('collectionEnabled',1),('lookbackPeriods',49),('quarterFallbacks',2),('quoteSide','bid_close'),
    ('extra',True),
])
def test_policy_does_not_coerce_or_accept_extra_fields(field,bad):
    value=policy();value[field]=bad
    with pytest.raises(ValueError):load('householdCredit',value)


@pytest.mark.parametrize('field,bad',[
    ('maxHttpPerRun',9),('maxHttpPerPeriod',9),('maxRetries',1),('maxResponseBytes',5242881),
    ('requestsPerMinute',11),('requestTimeoutSeconds',21),('maxResponseRows',65),('maxDirectoryCandidates',3),
])
def test_closed_physical_limits(field,bad):
    value=policy();value['limits'][field]=bad
    with pytest.raises(ValueError):load('householdCredit',value)


def test_group_caps_cannot_be_borrowed_or_leave_an_unfunded_request():
    for group in ('householdCredit','tier1Housing','specialBonds','propertyLoans'):
        value=policy(group);value['limits']['maxHttpPerPeriod']+=1
        with pytest.raises(ValueError):load(group,value)
        value=policy(group);value['limits']['maxHttpPerPeriod']=len(value['documents'])-1
        with pytest.raises(ValueError):load(group,value)


@pytest.mark.parametrize('group,mutation',[
    ('householdCredit',{'url':'https://example.org/report.html'}),
    ('householdCredit',{'period':'2026-02-30'}),
    ('householdCredit',{'derivedFrom':'other'}),
    ('householdCredit',{'role':'special-briefing'}),
    ('tier1Housing',{'url':'https://www.stats.gov.cn/xxgk/jd/sjjd2020/202610/t20261019_1965610.html?token=private'}),
    ('specialBonds',{'period':'2025'}),
    ('propertyLoans',{'url':'http://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html'}),
])
def test_exact_source_roles_dates_and_closed_https(group,mutation):
    value=policy(group);value['documents'][0].update(mutation)
    with pytest.raises(ValueError):load(group,value)


def test_distinct_adjacent_periods_and_no_cross_group_role_mix():
    value=policy();value['documents'][1]['period']='2026-06'
    with pytest.raises(ValueError):load('householdCredit',value)
    value=policy();value['documents'][1]=deepcopy(value['documents'][0])
    with pytest.raises(ValueError):load('householdCredit',value)
    value=policy('specialBonds');value['documents'][1]['period']='2025-08'
    with pytest.raises(ValueError):load('specialBonds',value)


def test_property_report_is_only_derived_from_its_actual_directory():
    value=policy('propertyLoans')
    for mutation in [{'derivedFrom':'property-report'},{'derivedFrom':None},{'url':'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026101417073743604/index.html'}]:
        bad=deepcopy(value);bad['documents'][1].update(mutation)
        with pytest.raises(ValueError):load('propertyLoans',bad)
