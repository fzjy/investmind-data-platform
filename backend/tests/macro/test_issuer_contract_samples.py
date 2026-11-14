from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from backend.tests.macro.test_catalog_member_policy import NOW,value as base_value
from backend.tests.macro.test_enterprise_target_catalog import load


BANK={'fundCode':'512800','mediaType':'html','url':'https://www.fsfund.com/fund/512800/fundDetail.shtml'}
CONSUMER={'fundCode':'159928','mediaType':'html','url':'https://www.99fund.com/main/products/pofund/159928/fundgk.shtml'}


def value():
    v=base_value()
    v['targets'].append({'kind':'etf','code':'159928.SZ','label':'消费ETF','sourceIndexNameHint':'中证主要消费指数'})
    v['catalogEvidence']['targets'].append({'code':'159928.SZ','documents':[]})
    v['catalogEvidence'].update(sampleEnabled=False,sampleDocuments=[deepcopy(BANK),deepcopy(CONSUMER)])
    return v


def module():return import_module('app.macro.issuer_contract_samples')


def plan(tmp_path,v=None,at=NOW,code='512800'):
    return module().build_sample_plan(load(tmp_path,value() if v is None else v),code,at)


def test_optional_registration_keeps_old_values_and_plan_has_no_inferred_index(tmp_path):
    v=value();old=deepcopy(v)
    del old['catalogEvidence']['sampleEnabled'];del old['catalogEvidence']['sampleDocuments']
    a,b=load(tmp_path,old),load(tmp_path,v)
    assert a.fingerprint==b.fingerprint and a.official_policy==b.official_policy
    assert not a.catalog_member_policy.sampleEnabled and a.catalog_member_policy.sampleDocuments==[]
    p=plan(tmp_path);assert module().validate_sample_plan(p)
    assert p['document']==BANK and p['target']['code']=='512800.SH'
    assert p['limits']['maxHttpPerDay']==4 and p['limits']['maxRetries']==0
    assert not any('indexCode' in d for d in [p,p['document'],p['target']])
    later=plan(tmp_path,at=NOW+timedelta(days=1))
    assert module().sample_run_key(p)==module().sample_run_key(later)


@pytest.mark.parametrize('bad',['enabled-empty','flag','duplicate','extra','unknown','index','not-registered',
    'url','query','type','raw-ip','userinfo','mixed-existing'])
def test_unclosed_registration_rejected(tmp_path,bad):
    v=value();p=v['catalogEvidence'];doc=p['sampleDocuments'][0]
    if bad=='enabled-empty':p.update(sampleEnabled=True,sampleDocuments=[])
    elif bad=='flag':p['sampleEnabled']=1
    elif bad=='duplicate':p['sampleDocuments'].append(deepcopy(doc))
    elif bad=='extra':doc['maxHttpPerDay']=40
    elif bad=='unknown':doc['fundCode']='999999'
    elif bad=='index':doc['fundCode']='000905'
    elif bad=='not-registered':p['targets']=[s for s in p['targets'] if s['code']!='512800.SH']
    elif bad=='url':doc['url']='https://example.org/512800'
    elif bad=='query':doc['url']+='?redirect=http://127.0.0.1'
    elif bad=='type':doc['mediaType']='pdf'
    elif bad=='raw-ip':doc['url']='https://127.0.0.1/fund/512800/fundDetail.shtml'
    elif bad=='userinfo':doc['url']='https://user:secret@www.fsfund.com/fund/512800/fundDetail.shtml'
    elif bad=='mixed-existing':
        from backend.tests.macro.test_fund_contract_discovery import document
        p['discoveryDocuments']=[document('512800','399986')]
    with pytest.raises(ValueError):load(tmp_path,v)


@pytest.mark.parametrize('bad',['hash','target','document','limits','date','schema','extra'])
def test_frozen_plan_rejects_mutations(tmp_path,bad):
    from app.macro.extensions.plan import digest
    p=plan(tmp_path)
    if bad=='hash':p['fingerprint']='0'*64
    elif bad=='target':p['target']['code']='512800.SZ'
    elif bad=='document':p['document']['url']+='?x=1'
    elif bad=='limits':p['limits']['maxHttpPerDay']=5
    elif bad=='date':p['firstBusinessDate']='20261026'
    elif bad=='schema':p['schemaVersion']=True
    elif bad=='extra':p['indexCode']='399986'
    if bad!='hash':p['fingerprint']=digest({k:v for k,v in p.items() if k!='fingerprint'})
    with pytest.raises(ValueError):module().validate_sample_plan(p)
