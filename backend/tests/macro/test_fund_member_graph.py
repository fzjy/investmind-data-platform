from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from importlib import import_module

import pytest

from app.macro.extensions.plan import digest
from app.macro.fund_contract_discovery import build_discovery_plan,parse_fund_contract,run_key as discovery_key
from app.macro.official_members import parse_csi_members
from backend.tests.macro.test_fund_contract_discovery import NOW,value as discovery_value,body
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_official_members import row
from backend.tests.macro.official_xls_fixture import compound_workbook


def value():
    v=discovery_value();v['catalogEvidence'].update(memberGraphEnabled=False,memberGraphs=[
        {'code':'512660.SH','discoveryDocumentId':'fund-512660'},
        {'code':'515220.SH','discoveryDocumentId':'fund-515220'}])
    return v


def module():return import_module('app.macro.fund_member_graph')


def source_proof(catalog,document_id='fund-512660'):
    p=build_discovery_plan(catalog,document_id,NOW-timedelta(seconds=3));doc=p['document']
    raw=body(doc['fundCode'],p['target']['sourceIndexNameHint'],resource='123456')
    return {'runId':discovery_key(p),'plan':p,'rawId':1,'rawSha256':sha256(raw).hexdigest(),
        'retrievedAt':(NOW-timedelta(seconds=2)).isoformat(),'completedAt':(NOW-timedelta(seconds=1)).isoformat(),
        'normalized':parse_fund_contract(raw,doc['fundCode'],p['target']['sourceIndexNameHint'])}


def intent(tmp_path,code='512660.SH'):
    c=load(tmp_path,value());doc=next(g.discoveryDocumentId for g in c.catalog_member_policy.memberGraphs if g.code==code)
    return module().build_fund_graph_intent(c,code,source_proof(c,doc),NOW)


def csi_receipt(p,day='2026-10-22',count=3):
    m=module();leaf=m.build_fund_graph_leaf(p,'csi-members',NOW+timedelta(seconds=1))
    raw=compound_workbook(rows=[row(date=day.replace('-',''),index=p['indexCode'],code=f'{600000+i:06}') for i in range(count)])
    normalized=parse_csi_members(raw,p['indexCode'])
    return {'runId':m.fund_graph_run_key(p,'csi-members'),'plan':leaf,'rawId':2,'rawSha256':sha256(raw).hexdigest(),
        'retrievedAt':(NOW+timedelta(seconds=2)).isoformat(),'completedAt':(NOW+timedelta(seconds=3)).isoformat(),'normalized':normalized}


def test_optional_graph_policy_and_intent_preserve_previous_target_identity(tmp_path):
    v=value();old=deepcopy(v);old['catalogEvidence'].pop('memberGraphEnabled');old['catalogEvidence'].pop('memberGraphs')
    before=load(tmp_path,old);after=load(tmp_path,v)
    assert before.fingerprint==after.fingerprint and before.official_policy==after.official_policy
    from app.macro.catalog_member_store import build_catalog_member_plan
    assert build_catalog_member_plan(before,'000905.SH',NOW)==build_catalog_member_plan(after,'000905.SH',NOW)
    p=intent(tmp_path)
    assert p['target']['code']=='512660.SH' and p['indexCode']=='399967'
    assert p['sourceContract']['normalized']['downloadResourceKey']=='123456'
    assert p['limits']['maxHttpPerDay']==4 and p['limits']['maxSnapshotMembers']==10000
    assert module().validate_fund_graph_intent(p)


@pytest.mark.parametrize('bad',['flag','extra-budget','duplicate','index','unknown','old-bootstrap','missing-discovery','wrong-discovery','enabled-empty','extra-graph-key'])
def test_unknown_graph_or_proof_source_is_rejected(tmp_path,bad):
    v=value();p=v['catalogEvidence'];g=p['memberGraphs'][0]
    if bad=='flag':p['memberGraphEnabled']=1
    elif bad=='extra-budget':p['graphMaxHttp']=40
    elif bad=='duplicate':p['memberGraphs'].append(deepcopy(g))
    elif bad=='index':g['code']='000905.SH'
    elif bad=='unknown':g['code']='999999.SH'
    elif bad=='old-bootstrap':g['code']='512880.SH'
    elif bad=='missing-discovery':p['discoveryDocuments']=p['discoveryDocuments'][1:]
    elif bad=='wrong-discovery':g['discoveryDocumentId']='fund-515220'
    elif bad=='enabled-empty':p.update(memberGraphEnabled=True,memberGraphs=[])
    elif bad=='extra-graph-key':g['downloadResourceKey']='999999'
    with pytest.raises(ValueError):load(tmp_path,v)


@pytest.mark.parametrize('day,count',[('2026-10-22',3),('2026-09-30',17)])
def test_pcf_date_and_resource_are_only_derived_from_bound_actual_csi(day,count,tmp_path):
    m=module();p=intent(tmp_path);receipt=csi_receipt(p,day,count)
    leaf=m.build_fund_graph_leaf(p,'fund-pcf',NOW+timedelta(seconds=4),receipt)
    assert leaf['document']['url']==f'https://m.gtfund.com/cochin/etf/download/123456/{day.replace("-","")}'
    assert leaf['document']['tradingDate']==day and leaf['csiReceipt']==receipt
    assert m.validate_fund_graph_leaf(leaf)
    assert m.fund_graph_run_key(p,'fund-pcf')!=m.fund_graph_run_key(p,'csi-members')
    with pytest.raises(ValueError):m.build_fund_graph_leaf(p,'fund-pcf',NOW)


@pytest.mark.parametrize('bad',['fake-run','raw-id-bool','wrong-fund','wrong-hint','hash','future-source','clock','extra-key'])
def test_unbound_contract_shape_or_visibility_does_not_make_intent(tmp_path,bad):
    c=load(tmp_path,value());proof=source_proof(c)
    if bad=='fake-run':proof['runId']='00000000-0000-0000-0000-000000000000'
    elif bad=='raw-id-bool':proof['rawId']=True
    elif bad=='wrong-fund':proof['normalized']['fundCode']='512880'
    elif bad=='wrong-hint':proof['normalized']['trackingIndexName']='中证煤炭指数'
    elif bad=='hash':proof['rawSha256']='0'*64
    elif bad=='future-source':proof['completedAt']=(NOW+timedelta(seconds=1)).isoformat()
    elif bad=='extra-key':proof['members']=['600000.SH']
    with pytest.raises(ValueError):module().build_fund_graph_intent(c,'512660.SH',proof,NOW.replace(tzinfo=None) if bad=='clock' else NOW)


def test_rehashed_wrong_parent_date_or_source_still_fails_semantic_binding(tmp_path):
    m=module();p=intent(tmp_path);r=csi_receipt(p);leaf=m.build_fund_graph_leaf(p,'fund-pcf',NOW+timedelta(seconds=4),r)
    for mutate in [lambda x:x['document'].update(tradingDate='2026-10-21',url='https://m.gtfund.com/cochin/etf/download/123456/20261021'),
        lambda x:x['csiReceipt'].update(rawSha256='0'*64),
        lambda x:x['document'].update(subscriptionCode='999999',url='https://m.gtfund.com/cochin/etf/download/999999/20261022')]:
        bad=deepcopy(leaf);mutate(bad);bad['fingerprint']=digest({k:v for k,v in bad.items() if k!='fingerprint'})
        with pytest.raises(ValueError):m.validate_fund_graph_leaf(bad)
