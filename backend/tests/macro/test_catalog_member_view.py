from datetime import date,timedelta
from importlib import import_module

import pytest

from app.db.models.ops import JobRun
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_catalog_member_policy import NOW,plan,value
from backend.tests.macro.test_catalog_member_store import store
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_official_member_store import store as legacy_store
from backend.tests.macro.test_official_members import row
from backend.tests.macro.official_xls_fixture import compound_workbook
from backend.tests.macro.test_enterprise_rea import controlled
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis


def source_bytes(code='000905'):
    return compound_workbook(rows=[row(index=code,indexName='控制指数'),
        row(index=code,indexName='控制指数',code='000001',exchange='深圳证券交易所',exchangeEnglish='Shenzhen Stock Exchange')])


def complete(factory,p,at=NOW):
    s=store(factory);d=p['policy']['documents'][0];key=s.reserve(p,d,at)
    s.receive(key,source_bytes(p['target']['code'][:6]),at+timedelta(seconds=1));s.replay(key,at+timedelta(seconds=2));return key


def test_new_scope_and_legacy_frozen_bootstrap_are_both_retained(database,tmp_path):
    _,f=database;c=load(tmp_path,value())
    from app.macro.official_member_policy import OfficialMemberPolicy
    from app.macro.official_member_store import build_plan
    # 历史根只有原300；当前旧bootstrap还含证券ETF，不能反过来改写历史根。
    v=c.official_policy.model_dump();v.update(collectionEnabled=True,bootstrapTargetCodes=['000300.SH'],documents=v['documents'][:1])
    p=build_plan(OfficialMemberPolicy.model_validate(v),c.fingerprint,NOW-timedelta(days=1));s=legacy_store(f)
    key=s.reserve(p,p['policy']['documents'][0],NOW)
    s.receive(key,compound_workbook(),NOW+timedelta(seconds=1));s.replay(key,NOW+timedelta(seconds=2))
    complete(f,plan(tmp_path),NOW+timedelta(seconds=3))
    module=import_module('app.macro.enterprise_official_targets');w=module.load_official_witnesses(f,NOW+timedelta(seconds=10),c)
    assert {x['targetCode'] for x in w}=={'000300.SH','000905.SH'}
    assert all(x['catalogFingerprint']==c.fingerprint for x in w)


def test_exact_cutoff_never_includes_not_yet_normalized_new_source(database,tmp_path):
    _,f=database;c=load(tmp_path,value());complete(f,plan(tmp_path))
    module=import_module('app.macro.enterprise_official_targets')
    assert module.load_official_witnesses(f,NOW+timedelta(seconds=1),c)==[]
    assert len(module.load_official_witnesses(f,NOW+timedelta(seconds=2),c))==1


def test_withdrawn_registration_hides_new_proof_but_disabling_collection_does_not(database,tmp_path):
    _,f=database;v=value();c=load(tmp_path,v);complete(f,plan(tmp_path))
    module=import_module('app.macro.enterprise_official_targets');cutoff=NOW+timedelta(seconds=5)
    assert len(module.load_official_witnesses(f,cutoff,c))==1  # flag already false: readonly proof stays visible.
    v['catalogEvidence']['targets']=[s for s in v['catalogEvidence']['targets'] if s['code']!='000905.SH']
    withdrawn=load(tmp_path,v)
    assert withdrawn.fingerprint==c.fingerprint
    assert module.load_official_witnesses(f,cutoff,withdrawn)==[]
    assert module.load_official_source_states(f,cutoff,withdrawn)['000905.SH']['status']=='contract-pending'


def test_historical_received_state_is_stable_after_later_normalization(database,tmp_path):
    _,f=database;c=load(tmp_path,value());p=plan(tmp_path);s=store(f);doc=p['policy']['documents'][0]
    key=s.reserve(p,doc,NOW);s.receive(key,source_bytes(),NOW+timedelta(seconds=1))
    module=import_module('app.macro.enterprise_official_targets');cutoff=NOW+timedelta(seconds=2)
    before=module.load_official_source_states(f,cutoff,c)
    assert before['000905.SH']['status']=='replay-pending'
    s.replay(key,NOW+timedelta(seconds=3))
    assert module.load_official_source_states(f,cutoff,c)==before
    assert module.load_official_witnesses(f,cutoff,c)==[]


def test_catalog_comparison_includes_all_registered_targets_and_explicit_contract_pending(tmp_path):
    c=load(tmp_path,value());data=controlled(7);policy=EnterprisePolicy(min_samples=1);asof=date(2026,10,26)
    parent=public_enterprise_analysis(data,asof,policy,rea_enabled=True)
    module=import_module('app.macro.enterprise_official_targets')
    comparison=module.build_official_target_comparison(data,asof,policy,parent,c,[])
    assert comparison['schemaVersion']=='1.1' and [t['code'] for t in comparison['targets']]==[t.code for t in c.targets]
    states={t['code']:t['sourceState'] for t in comparison['targets']}
    assert states['512800.SH']['status']=='contract-pending'
    assert states['000905.SH']['status']=='not-collected'
    assert module.valid_official_target_comparison(comparison,parent)


def test_failed_new_target_has_named_source_state_without_hiding_others(database,tmp_path):
    _,f=database;c=load(tmp_path,value());p=plan(tmp_path);s=store(f)
    key=s.reserve(p,p['policy']['documents'][0],NOW);s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=1))
    complete(f,plan(tmp_path,'000510.SH'),NOW+timedelta(seconds=2))
    module=import_module('app.macro.enterprise_official_targets')
    states=module.load_official_source_states(f,NOW+timedelta(seconds=8),c)
    assert states['000905.SH']=={'status':'source-failed','errorCode':'EXTENSION_AUTH_ERROR'}
    assert states['000510.SH']['status']=='verified'
    assert states['512800.SH']['status']=='contract-pending'


def test_scope_size_follows_pool_not_fixed_stock_count(tmp_path):
    from copy import deepcopy
    from backend.tests.macro.test_enterprise_official_targets import witness
    c=load(tmp_path,value());data=controlled(17);policy=EnterprisePolicy(min_samples=1);asof=date(2026,10,26)
    parent=public_enterprise_analysis(data,asof,policy,rea_enabled=True)
    w=witness(list(data.symbols[:5]),code='000905.SH');w['indexCode']='000905';w['catalogFingerprint']=c.fingerprint
    w['documents'][0]['url']=w['documents'][0]['url'].replace('000300','000905')
    module=import_module('app.macro.enterprise_official_targets')
    comparison=module.build_official_target_comparison(data,asof,policy,parent,c,[w]);t=next(t for t in comparison['targets'] if t['code']=='000905.SH')
    assert t['status']=='available' and t['analysis']['poolCount']==5 and parent['poolCount']==17
    assert module.valid_official_target_comparison(comparison,parent)
    for mutate in [lambda x:x['targets'][0]['sourceState'].update(status='invented'),
        lambda x:x['targets'][-1]['sourceState'].update(status='verified'),
        lambda x:x['targets'][0]['sourceState'].update(errorCode='secret body')]:
        damaged=deepcopy(comparison);mutate(damaged)
        assert not module.valid_official_target_comparison(damaged,parent)
