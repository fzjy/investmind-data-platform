from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module

import pytest

from backend.tests.macro.test_enterprise_target_catalog import payload,load
from backend.tests.macro.test_official_member_policy import policy as original_policy

NOW=datetime(2026,10,26,2,tzinfo=timezone.utc)


def source(code):
    return {'id':'csi-'+code,'kind':'csi-members','indexCode':code,'fundCode':None,'subscriptionCode':None,'tradingDate':None,
        'url':f'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{code}cons.xls'}


def value():
    v=payload();v['maxTargetsPerPreview']=24;v['officialEvidence']=original_policy()
    v['targets'] += [
        {'kind':'index','code':c+'.SH','label':n,'sourceIndexNameHint':None} for c,n in [('000905','中证500'),('000510','中证A500')]]
    v['targets'].append({'kind':'etf','code':'512800.SH','label':'银行ETF','sourceIndexNameHint':'中证银行指数'})
    v['catalogEvidence']={'collectionEnabled':False,'limitsReference':'officialEvidence','targets':[
        {'code':'000905.SH','documents':[source('000905')]},
        {'code':'000510.SH','documents':[source('000510')]},
        {'code':'512800.SH','documents':[]}]}
    return v


def plan(tmp_path,code='000905.SH',v=None,now=NOW):
    catalog=load(tmp_path,value() if v is None else v)
    return import_module('app.macro.catalog_member_store').build_catalog_member_plan(catalog,code,now)


def test_optional_section_preserves_legacy_catalog_and_bootstrap_policies(tmp_path):
    v=value();without=deepcopy(v);without.pop('catalogEvidence')
    old=load(tmp_path,without);current=load(tmp_path,v)
    assert current.fingerprint==old.fingerprint and current.official_policy==old.official_policy
    assert current.evidence_policy==old.evidence_policy
    p=current.catalog_member_policy
    assert not p.collectionEnabled and p.limitsReference=='officialEvidence'
    assert [t.code for t in p.targets]==['000905.SH','000510.SH','512800.SH']


@pytest.mark.parametrize('mutation',['root-extra','flag-int','limit-reference','duplicate','old-bootstrap','unknown','wrong-index','missing-index','arbitrary-url','duplicate-doc','missing-base','empty-targets','extra-target-key','etf-incomplete'])
def test_unknown_scope_or_new_budget_and_incomplete_declared_sources_rejected(tmp_path,mutation):
    v=value();p=v['catalogEvidence'];t=p['targets'][0]
    if mutation=='root-extra':p['maxHttpPerDay']=40
    elif mutation=='flag-int':p['collectionEnabled']=1
    elif mutation=='limit-reference':p['limitsReference']='arbitrary'
    elif mutation=='duplicate':p['targets'].append(deepcopy(t))
    elif mutation=='old-bootstrap':t['code']='000300.SH';t['documents']=[source('000300')]
    elif mutation=='unknown':t['code']='000906.SH'
    elif mutation=='wrong-index':t['documents']=[source('000510')]
    elif mutation=='missing-index':t['documents']=[]
    elif mutation=='arbitrary-url':t['documents'][0]['url']='https://untrusted.invalid/000905.xls'
    elif mutation=='duplicate-doc':t['documents'].append(deepcopy(t['documents'][0]))
    elif mutation=='missing-base':v.pop('officialEvidence')
    elif mutation=='empty-targets':p['targets']=[]
    elif mutation=='extra-target-key':t['ready']=True
    else:p['targets'][2]['documents']=[source('399986')]
    with pytest.raises(ValueError):load(tmp_path,v)


def test_per_target_plan_freezes_only_that_target_and_shared_original_limits(tmp_path):
    p=plan(tmp_path)
    assert p['schemaVersion']==2 and p['target']['code']=='000905.SH' and p['businessDate']=='2026-10-26'
    assert p['policy']['bootstrapTargetCodes']==['000905.SH'] and p['policy']['maxHttpPerDay']==4
    assert p['policy']['documents']==[source('000905')]
    v=value();v['catalogEvidence']['collectionEnabled']=True
    assert plan(tmp_path,v=v)==p
    v['catalogEvidence']['targets'][1]['documents'][0]['id']='different-document-id'
    assert plan(tmp_path,v=v)==p
    assert plan(tmp_path,'000510.SH',v=v)['fingerprint']!=p['fingerprint']
    import_module('app.macro.catalog_member_store').validate_catalog_member_plan(p)


def test_empty_etf_contract_never_creates_executable_plan(tmp_path):
    with pytest.raises(ValueError):plan(tmp_path,'512800.SH')


@pytest.mark.parametrize('field',['target','url','budget','extra','timestamp','policy-hash','catalog-hash'])
def test_rehashing_plan_cannot_expand_target_source_or_limits(tmp_path,field):
    p=plan(tmp_path)
    if field=='target':p['target']['code']='000510.SH'
    elif field=='url':p['policy']['documents'][0]['url']='https://untrusted.invalid/x.xls'
    elif field=='budget':p['policy']['maxHttpPerDay']=40
    elif field=='extra':p['other']=True
    elif field=='timestamp':p['businessDate']='2026-10-32'
    elif field=='policy-hash':p['policyHash']='0'*64
    else:p['catalogFingerprint']='not-a-hash'
    from app.macro.extensions.plan import digest
    p['fingerprint']=digest({k:v for k,v in p.items() if k!='fingerprint'})
    with pytest.raises(ValueError):import_module('app.macro.catalog_member_store').validate_catalog_member_plan(p)


def test_naive_clock_rejected(tmp_path):
    with pytest.raises(ValueError):plan(tmp_path,now=NOW.replace(tzinfo=None))
