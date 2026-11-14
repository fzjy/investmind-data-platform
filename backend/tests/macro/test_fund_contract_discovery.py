from copy import deepcopy
import json
from importlib import import_module

import pytest

from backend.tests.macro.test_catalog_member_policy import value as catalog_value,NOW
from backend.tests.macro.test_enterprise_target_catalog import load


def document(code='512660',index='399967'):
    return {'id':'fund-'+code,'kind':'fund-mapping','indexCode':index,'fundCode':code,'subscriptionCode':None,
        'tradingDate':None,'url':f'https://e.gtfund.com/etrade/Jijin/view/id/{code}#etf-list'}


def value():
    v=catalog_value()
    for code,name in [('512660','中证军工指数'),('515220','中证煤炭指数')]:
        v['targets'].append({'kind':'etf','code':code+'.SH','label':name,'sourceIndexNameHint':name})
        v['catalogEvidence']['targets'].append({'code':code+'.SH','documents':[]})
    v['catalogEvidence'].update(discoveryEnabled=False,discoveryDocuments=[document(),document('515220','399998')])
    return v


def body(code='512660',name='中证军工指数',resource='512661'):
    detail=json.dumps({'code':code,'etf':[{'code':resource,'fundCode':code}]})
    return (f'<html><h3>军工ETF国泰<span>({code})</span></h3>'
        f'<p>本基金的标的指数为{name}及其未来可能发生的变更。</p>'
        f'<select class="J_etf_fundcode"><option value="{resource}">申购赎回清单</option></select>'
        f'<script>var gtOptions = {{\n fundcode: \'{code}\',\n detail: {detail},\n fund_status: "created"}};</script></html>').encode()


def module():return import_module('app.macro.fund_contract_discovery')


def plan(tmp_path,code='fund-512660',v=None,at=NOW):
    return module().build_discovery_plan(load(tmp_path,value() if v is None else v),code,at)


def test_discovery_registration_preserves_catalog_and_existing_scope_plans(tmp_path):
    v=value();old=deepcopy(v);old['catalogEvidence'].pop('discoveryEnabled');old['catalogEvidence'].pop('discoveryDocuments')
    before=load(tmp_path,old);after=load(tmp_path,v)
    assert before.fingerprint==after.fingerprint and before.official_policy==after.official_policy
    from app.macro.catalog_member_store import build_catalog_member_plan
    assert build_catalog_member_plan(before,'000905.SH',NOW)==build_catalog_member_plan(after,'000905.SH',NOW)
    p=plan(tmp_path)
    assert p['document']['fundCode']=='512660' and p['target']['code']=='512660.SH'
    assert p['limits']['maxHttpPerDay']==4 and p['limits']['maxRetries']==0
    assert module().validate_discovery_plan(p)


@pytest.mark.parametrize('bad',['flag','unknown-key','duplicate-id','duplicate-fund','index','unknown-target','unregistered',
    'wrong-domain','pcf','enabled-empty'])
def test_discovery_rejects_unclosed_registration(tmp_path,bad):
    v=value();p=v['catalogEvidence'];d=p['discoveryDocuments'][0]
    if bad=='flag':p['discoveryEnabled']=1
    elif bad=='unknown-key':p['discoveryMaxHttp']=40
    elif bad=='duplicate-id':p['discoveryDocuments'][1]['id']=d['id']
    elif bad=='duplicate-fund':p['discoveryDocuments'].append({**d,'id':'another'})
    elif bad=='index':d.update(fundCode='000905',url='https://e.gtfund.com/etrade/Jijin/view/id/000905#etf-list')
    elif bad=='unknown-target':d.update(fundCode='999999',url='https://e.gtfund.com/etrade/Jijin/view/id/999999#etf-list')
    elif bad=='unregistered':p['targets']=[t for t in p['targets'] if t['code']!='512660.SH']
    elif bad=='wrong-domain':d['url']='https://example.org/512660'
    elif bad=='pcf':d.update(kind='fund-pcf',subscriptionCode='512661',tradingDate='2026-10-23',url='https://m.gtfund.com/cochin/etf/download/512661/20261023')
    elif bad=='enabled-empty':p.update(discoveryEnabled=True,discoveryDocuments=[])
    with pytest.raises(ValueError):load(tmp_path,v)


def test_resource_key_is_extracted_from_verified_product_not_numeric_arithmetic():
    result=module().parse_fund_contract(body(resource='123456'),'512660','中证军工指数')
    assert result['downloadResourceKey']=='123456' and result['fundCode']=='512660'
    assert result['trackingIndexName']=='中证军工指数' and len(result['rawSha256'])==64
    assert 'members' not in result and 'subscriptionCode' not in result


def test_resource_for_another_fund_or_unbound_select_is_rejected():
    wrong=body().replace(b'"fundCode": "512660"',b'"fundCode": "512880"')
    unbound=body().split(b'<script>')[0]+b'</html>'
    panel=unbound.replace(b'<select',b'<div data-fund-code="512880"><select').replace(b'</select>',b'</select></div>')
    for candidate in (wrong,unbound,panel):
        with pytest.raises(ValueError):module().parse_fund_contract(candidate,'512660','中证军工指数')


@pytest.mark.parametrize('bad',[
    body('512880'),body(name='中证煤炭指数'),body(resource='51266x'),
    body().replace(b'</select>',b'<option value="512662">another</option></select>'),
    body()+b'<select class="J_etf_fundcode"><option value="512661">duplicate</option></select>',
    body().replace(b'J_etf_fundcode',b'other-class'),b'<script>'+body()+b'</script>',
    body().replace(b'<select',b'<script></script><style><select').replace(b'</select>',b'</select></style>'),
    b'\xff',b'',
])
def test_wrong_identity_or_ambiguous_hidden_resource_does_not_close_contract(bad):
    with pytest.raises(ValueError):module().parse_fund_contract(bad,'512660','中证军工指数')


def test_plan_tampering_and_nonaware_clock_fail_closed(tmp_path):
    p=plan(tmp_path)
    for field in ['fingerprint','catalogFingerprint','firstBusinessDate']:
        bad=deepcopy(p);bad[field]='invalid'
        with pytest.raises(ValueError):module().validate_discovery_plan(bad)
    with pytest.raises(ValueError):plan(tmp_path,at=NOW.replace(tzinfo=None))
