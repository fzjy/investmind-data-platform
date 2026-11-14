from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from app.db.models.ops import JobRun
from app.macro.extensions.plan import digest
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_issuer_contract_samples import NOW,value
from backend.tests.macro.test_enterprise_target_catalog import load
from app.macro.issuer_contract_samples import SOURCES,build_sample_plan
from app.macro.issuer_sample_store import IssuerSampleStore


def seed(factory,tmp_path,code='512400',failed=True):
    v=value();v['catalogEvidence']['sampleDocuments']=[]
    if code not in {t['code'][:6] for t in v['targets']}:
        v['targets'].append({'kind':'etf','code':code+'.SH','label':'测试ETF','sourceIndexNameHint':'测试跟踪指数'})
        v['catalogEvidence']['targets'].append({'code':code+'.SH','documents':[]})
    kind,url,_=SOURCES[code]
    v['catalogEvidence'].update(collectionEnabled=True,sampleEnabled=True,sampleDocuments=[{'fundCode':code,'mediaType':kind,'url':url}])
    recovery_url='https://www.gffunds.com.cn/proxy/pcflist/512980' if code=='512980' else url
    v['catalogEvidence']['sampleRecoveries']=[{'fundCode':code,'mediaType':'html','url':recovery_url}]
    v['officialEvidence']['dailyBudgetEnabled']=False
    c=load(tmp_path,v);s=IssuerSampleStore(factory,daily_budget_enabled=False)
    p=build_sample_plan(c,code,NOW);key=s.reserve(p,NOW)
    if failed:s.fail(key,'EXTENSION_HTTP_ERROR' if code=='512980' else 'EXTENSION_ENCODING_UNSUPPORTED',NOW+timedelta(seconds=1))
    return c,key


def api():return import_module('app.macro.issuer_sample_recovery')
def store(factory):return import_module('app.macro.issuer_recovery_store').IssuerRecoveryStore(factory,daily_budget_enabled=False)


@pytest.mark.parametrize('code',['512400','512200','512980'])
def test_fixed_recovery_slot_preserves_failed_parent_and_receives_only_raw(database,tmp_path,code):
    _,f=database;c,parent=seed(f,tmp_path,code);at=NOW+timedelta(seconds=2)
    p=api().build_recovery_plan(f,c,code,at)
    assert p['parent']['runId']==parent and p['parent']['errorCode']
    assert p['transportPolicy']=='identity-or-single-gzip-bounded-v1'
    before={}
    with f() as session:before=deepcopy(session.get(JobRun,parent).details)
    s=store(f);key=s.reserve(p,at);s.receive(key,b'<html>original recovered response</html>',at+timedelta(seconds=1))
    later=api().build_recovery_plan(f,c,code,at+timedelta(days=1))
    state=s.inspect(later,at+timedelta(days=1))
    assert state['phase']=='received' and state['plan']==p and state['proofReady'] is False
    assert state['sample']['rawId']>0 and state['normalized'] is None
    with pytest.raises(ValueError):s.reserve(later,at+timedelta(days=1))
    with f() as session:assert session.get(JobRun,parent).details==before and session.get(JobRun,parent).status=='failed'
    assert count(f,JobRun)==2


def test_unfailed_parent_cannot_be_used_for_recovery(database,tmp_path):
    _,f=database;c,_=seed(f,tmp_path,failed=False)
    with pytest.raises(ValueError):api().build_recovery_plan(f,c,'512400',NOW+timedelta(seconds=2))


@pytest.mark.parametrize('field',['url','parent','transport','limits'])
def test_plan_tampering_rejected_even_with_rehashed_envelope(database,tmp_path,field):
    _,f=database;c,_=seed(f,tmp_path);p=api().build_recovery_plan(f,c,'512400',NOW+timedelta(seconds=2))
    if field=='url':p['document']['url']='https://example.org/arbitrary'
    elif field=='parent':p['parent']['detailsHash']='0'*64
    elif field=='transport':p['transportPolicy']='unlimited-gzip'
    else:p['limits']['maxResponseBytes']=8388609
    p['fingerprint']=digest({k:v for k,v in p.items() if k!='fingerprint'})
    with pytest.raises(ValueError):store(f).reserve(p,NOW+timedelta(seconds=2))


def test_parent_change_after_reservation_blocks_receive(database,tmp_path):
    _,f=database;c,parent=seed(f,tmp_path);at=NOW+timedelta(seconds=2)
    p=api().build_recovery_plan(f,c,'512400',at);s=store(f);key=s.reserve(p,at)
    with f.begin() as session:session.get(JobRun,parent).finished_at=NOW+timedelta(seconds=2)
    with pytest.raises(ValueError):s.receive(key,b'not accepted',at+timedelta(seconds=1))


@pytest.mark.parametrize('failed',[False,True])
def test_unknown_and_failed_recovery_slots_are_not_reset_next_day(database,tmp_path,failed):
    _,f=database;c,_=seed(f,tmp_path);at=NOW+timedelta(seconds=2)
    p=api().build_recovery_plan(f,c,'512400',at);s=store(f);key=s.reserve(p,at)
    if failed:s.fail(key,'EXTENSION_NETWORK_ERROR',at+timedelta(seconds=1))
    later=api().build_recovery_plan(f,c,'512400',at+timedelta(days=1))
    state=s.inspect(later,at+timedelta(days=1))
    assert state['phase']==('failed' if failed else 'reserved')
    with pytest.raises(ValueError):s.reserve(later,at+timedelta(days=1))
    assert count(f,JobRun)==2


def test_recovered_raw_is_not_a_membership_witness(database,tmp_path):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    _,f=database;c,_=seed(f,tmp_path);at=NOW+timedelta(seconds=2)
    p=api().build_recovery_plan(f,c,'512400',at);s=store(f);key=s.reserve(p,at)
    s.receive(key,b'<html>not a membership proof</html>',at)
    assert load_catalog_member_witnesses(f,at,c)==[]


@pytest.mark.parametrize('bad',['url','query','unknown','duplicate','missing-parent-registration'])
def test_recovery_configuration_is_closed_and_bound_to_original_samples(tmp_path,bad):
    v=value();doc={'fundCode':'512400','mediaType':'html','url':SOURCES['512400'][1]}
    v['targets'].append({'kind':'etf','code':'512400.SH','label':'测试ETF','sourceIndexNameHint':'测试跟踪指数'})
    v['catalogEvidence']['targets'].append({'code':'512400.SH','documents':[]})
    v['catalogEvidence']['sampleDocuments'].append(deepcopy(doc))
    v['catalogEvidence']['sampleRecoveries']=[doc]
    if bad=='url':doc['url']='https://example.org/unregistered'
    elif bad=='query':doc['url']+='&force=1'
    elif bad=='unknown':doc['fundCode']='512800'
    elif bad=='duplicate':v['catalogEvidence']['sampleRecoveries'].append(deepcopy(doc))
    else:v['catalogEvidence']['sampleDocuments']=[]
    with pytest.raises(ValueError):load(tmp_path,v)
