from copy import deepcopy
from importlib import import_module

import pytest


def policy(group='marginBalances'):
    margin=group=='marginBalances'
    requests=([{'id':'margin-'+e.lower(),'params':{'exchange_id':e}} for e in ('SSE','SZSE','BSE')] if margin else
        [{'id':'float-sh-a','params':{'ts_code':'SH_A','exchange':'SH'}},{'id':'float-sz-a','params':{'ts_code':'SZ_A','exchange':'SZ'}}])
    return {'collectionEnabled':False,'schedule':{'frequency':'daily','weekday':0,'time':'19:00','timezone':'Asia/Shanghai'},
        'lookbackPeriods':48,'quoteSide':None,'publicationLagTradingDays':1,'publicationTime':'10:00','publicationTimezone':'Asia/Shanghai',
        'calendarLookbackDays':45,'calendarLookaheadDays':14,'calendarExchanges':['SSE','SZSE'],
        'providerUrl':'https://api.tushare.pro','documentationUrl':'https://tushare.pro/document/2?doc_id='+('58' if margin else '215'),
        'useScope':'personal-local-noncommercial','apiName':'margin' if margin else 'daily_info',
        'fields':['trade_date','exchange_id','rzye'] if margin else ['trade_date','ts_code','exchange','float_mv'],
        'unit':'元' if margin else '亿元','requests':requests,
        'limits':{'maxHttpPerRun':len(requests),'maxHttpPerPeriod':len(requests),'requestsPerMinute':10,
            'requestTimeoutSeconds':20,'maxRetries':0,'maxResponseBytes':1048576,'maxResponseRows':1}}


def validate(group,value=None):
    return import_module('app.macro.extensions.config').validate_group(group,policy(group) if value is None else value)


@pytest.mark.parametrize('group',['marginBalances','aShareFloatMv'])
def test_closed_independent_policies_and_minimum_aggregate_requests(group):
    value=validate(group)
    assert value.limits.maxHttpPerPeriod==len(value.requests) and value.publicationLagTradingDays==1
    assert value.schedule.time=='19:00' and value.publicationTime=='10:00'
    assert value.calendarExchanges==['SSE','SZSE'] and value.lookbackPeriods==48


@pytest.mark.parametrize('field,bad',[
    ('collectionEnabled',1),('lookbackPeriods',49),('lookbackPeriods',True),
    ('publicationLagTradingDays',0),('publicationLagTradingDays',4),('publicationTime','9:05'),('publicationTimezone','UTC'),
    ('calendarLookbackDays',0),('calendarLookaheadDays',32),('calendarExchanges',['SSE']),
    ('calendarExchanges',['SSE','BSE']),('quoteSide','close'),('unit','亿元'),('apiName','margin_detail'),
    ('providerUrl','http://api.tushare.pro'),('providerUrl','https://example.org'),
    ('documentationUrl','https://tushare.pro/document/2?doc_id=215'),('useScope','public-commercial'),('extra',True),
    ('fields',['trade_date','exchange_id','rzrqye']),('fields',['trade_date','exchange_id','rzye','token']),
])
def test_invalid_policy_fails_closed_without_echoing_values(field,bad):
    value=policy();value[field]=bad
    with pytest.raises(ValueError,match='^EXTENSION_CONFIG_INVALID$'):validate('marginBalances',value)


@pytest.mark.parametrize('field,bad',[
    ('maxHttpPerRun',4),('maxHttpPerPeriod',4),('maxRetries',1),('maxResponseBytes',1048577),
    ('maxResponseRows',2),('requestsPerMinute',11),('requestTimeoutSeconds',21),
])
def test_single_row_and_no_implicit_extra_budget(field,bad):
    value=policy();value['limits'][field]=bad
    with pytest.raises(ValueError):validate('marginBalances',value)


def test_requests_cannot_borrow_scopes_duplicate_or_add_dates_and_pagination():
    for bad in [
        [{'id':'one','params':{'exchange_id':'SSE'}}],
        [*policy()['requests'],{'id':'more','params':{'exchange_id':'SSE'}}],
        [policy()['requests'][0],policy()['requests'][0],policy()['requests'][2]],
    ]:
        value=policy();value['requests']=bad
        with pytest.raises(ValueError):validate('marginBalances',value)
    for key,bad in [('trade_date','20261022'),('start_date','20200101'),('offset','0'),('limit','4000'),('ts_code','600000.SH')]:
        value=policy();value['requests'][0]['params'][key]=bad
        with pytest.raises(ValueError):validate('marginBalances',value)


def test_a_share_denominator_only_includes_whole_sh_and_sz_a_market_not_extra_board():
    for field,bad in [('ts_code','SH_STAR'),('ts_code','SZ_GEM'),('ts_code','SH_MARKET'),('exchange','BSE')]:
        value=policy('aShareFloatMv');value['requests'][0]['params'][field]=bad
        with pytest.raises(ValueError):validate('aShareFloatMv',value)


def test_field_order_can_change_without_changing_semantic_required_fields():
    value=policy();value['fields'].reverse()
    assert validate('marginBalances',value).fields==value['fields']


def test_new_policies_fit_one_existing_extension_config_without_dependency_on_each_other():
    module=import_module('app.macro.extensions.config')
    old=module.load_extension_config().model_dump()
    for group in ('marginBalances','aShareFloatMv'):
        value=deepcopy(old);value['groups'][group]=policy(group)
        checked=module.ExtensionConfig.model_validate(value)
        assert set(checked.groups)=={*old['groups'],group}


def test_old_eight_group_literal_fingerprints_survive_shared_model_extraction():
    from datetime import datetime,timezone
    from app.macro.extensions.config import load_extension_config,ExtensionConfig
    from app.macro.extensions.plan import build_extension_plan
    from backend.tests.macro.test_official_macro_config import policy as original_official_policy
    expected={
        'usdcnh':'752910bf7c3e5dae39c8cb55fa879047badb8b5f345f59480ba2ba18509ef0f7',
        'vix':'2d7d515a4402f7fa99f1b49cbdd468da5646c0cadb0cb4822b11c364837c7daa',
        'governmentBonds':'b70750aa1ae224071d320a2aca35f3e0247dc820adcd9929162a4a91f12abc04',
        'householdCredit':'db86dc26dc4d8ac32941b1ed08fdfdc27b0bff3cb346d8d1464642939c472ea9',
        'tier1Housing':'dafa7d02133d76a222170d2016cc0edabab30011242c5602e7fcb18822b9d35b',
        'specialBonds':'8b8981b7d2f6f86d31accfe5fcdf8f4c7a6cd2d7f1860224e65071bebdfbf3da',
        'propertyLoans':'0dd65ff504d6a37dabdfb1cc94a9fff0b3e63de7f1f00aec90be7f1fa9e8880d',
        'nominalGdp':'3cb50ae0547b29d7c0bf7f0d942b637ffc19eb39ccf47a1fcda689097f71c4c1'}
    # 本例验证历史静态合同；运行配置已经正常迁移目录图，不能据此重写旧摘要。
    frozen=load_extension_config().model_dump()
    frozen['groups']['householdCredit']=original_official_policy('householdCredit')
    frozen['groups']['specialBonds']=original_official_policy('specialBonds')
    frozen['groups']['tier1Housing']=original_official_policy('tier1Housing')
    config=ExtensionConfig.model_validate(frozen)
    assert {key:build_extension_plan(key,config,datetime(2026,10,25,6,tzinfo=timezone.utc))['fingerprint'] for key in expected}==expected
