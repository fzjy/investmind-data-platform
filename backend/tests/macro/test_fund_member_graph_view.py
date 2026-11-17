from datetime import timedelta
from importlib import import_module

from app.db.models.ops import JobRun
from app.macro.enterprise_official_targets import load_official_witnesses,load_official_source_states,valid_witness
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_fund_member_graph import NOW,value
from backend.tests.macro.test_fund_member_graph_store import setup,store,complete_csi
from backend.tests.macro.test_official_pcf import body as pcf_body
from backend.tests.macro.test_enterprise_target_catalog import load


def finish(factory,tmp_path):
    c,p=setup(factory,tmp_path);s=store(factory);complete_csi(s,p)
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4));s.receive(key,pcf_body(code='512660',day='20261022'),NOW+timedelta(seconds=5))
    s.replay(key,NOW+timedelta(seconds=6));return c,p,key


def test_metadata_and_csi_alone_do_not_prove_fund_but_three_receipts_do(database,tmp_path):
    _,f=database;c,p=setup(f,tmp_path)
    assert load_official_witnesses(f,NOW,c)==[]
    s=store(f);complete_csi(s,p)
    assert load_official_witnesses(f,NOW+timedelta(seconds=3),c)==[]
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4));s.receive(key,pcf_body(code='512660',day='20261022'),NOW+timedelta(seconds=5));s.replay(key,NOW+timedelta(seconds=6))
    w=load_official_witnesses(f,NOW+timedelta(seconds=7),c)
    assert len(w)==1 and valid_witness(w[0],'512660.SH','etf',(NOW+timedelta(seconds=7)).date())
    assert w[0]['members']==['000001.SZ','600000.SH'] and [d['kind'] for d in w[0]['documents']]==['csi-members','fund-mapping','fund-pcf']


def test_exact_cutoff_received_state_does_not_regress_after_normalization(database,tmp_path):
    _,f=database;c,p=setup(f,tmp_path);s=store(f);complete_csi(s,p)
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4));s.receive(key,pcf_body(code='512660',day='20261022'),NOW+timedelta(seconds=5))
    at=NOW+timedelta(seconds=5);before=load_official_source_states(f,at,c)
    assert before['512660.SH']['status']=='replay-pending'
    s.replay(key,NOW+timedelta(seconds=6))
    assert load_official_source_states(f,at,c)==before and load_official_witnesses(f,at,c)==[]


def test_collection_disabled_keeps_proof_but_withdrawn_graph_hides_it(database,tmp_path):
    _,f=database;c,_,_=finish(f,tmp_path)
    assert not c.catalog_member_policy.memberGraphEnabled and len(load_official_witnesses(f,NOW+timedelta(seconds=7),c))==1
    v=value();v['catalogEvidence']['memberGraphs']=v['catalogEvidence']['memberGraphs'][1:]
    withdrawn=load(tmp_path,v)
    assert load_official_witnesses(f,NOW+timedelta(seconds=7),withdrawn)==[]
    assert load_official_source_states(f,NOW+timedelta(seconds=7),withdrawn)['512660.SH']['status']=='contract-pending'


def test_failed_graph_has_named_failure_and_never_fills_member_scope(database,tmp_path):
    _,f=database;c,p=setup(f,tmp_path);s=store(f);key=s.reserve(p,'csi-members',NOW+timedelta(seconds=1))
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=2))
    states=load_official_source_states(f,NOW+timedelta(seconds=3),c)
    assert states['512660.SH']=={'status':'source-failed','errorCode':'EXTENSION_AUTH_ERROR'}
    assert load_official_witnesses(f,NOW+timedelta(seconds=3),c)==[]


def test_uninitialized_optional_ops_storage_does_not_invent_proof_or_crash(tmp_path, pg_url):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    c=load(tmp_path,value());engine=create_engine(pg_url)
    try:
        witnesses,states=import_module('app.macro.fund_member_graph_view').load_fund_graph_view(sessionmaker(engine),NOW,c)
        assert witnesses==[] and all(s['status']=='not-collected' for s in states.values())
        assert load_official_witnesses(sessionmaker(engine),NOW,c)==[]
    finally:engine.dispose()


def test_parent_received_historical_status_stays_same_after_parent_completion(database,tmp_path):
    from app.macro.fund_contract_discovery import build_discovery_plan
    from app.macro.fund_contract_store import FundContractStore
    from backend.tests.macro.test_fund_contract_discovery import body
    _,f=database;c=load(tmp_path,value());s=FundContractStore(f);p=build_discovery_plan(c,'fund-512660',NOW);key=s.reserve(p,NOW)
    s.receive(key,body(),NOW+timedelta(seconds=1));cutoff=NOW+timedelta(seconds=2)
    before=load_official_source_states(f,cutoff,c)
    assert before['512660.SH']['status']=='replay-pending'
    s.replay(key,NOW+timedelta(seconds=3))
    assert load_official_source_states(f,cutoff,c)==before and load_official_witnesses(f,cutoff,c)==[]
