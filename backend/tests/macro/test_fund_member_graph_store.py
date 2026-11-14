from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import pytest

from app.db.models.ops import JobRun
from app.db.models.raw import IngestionBatch,SourcePayload
from app.macro.extensions.plan import digest
from app.macro.fund_contract_discovery import build_discovery_plan
from app.macro.fund_contract_store import FundContractStore
from app.macro.fund_member_graph import build_fund_graph_intent,validate_fund_graph_intent
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_fund_member_graph import value,NOW
from backend.tests.macro.test_fund_contract_discovery import body as mapping_body
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_official_members import row
from backend.tests.macro.official_xls_fixture import compound_workbook
from backend.tests.macro.test_official_pcf import body as pcf_body


def module():return import_module('app.macro.fund_member_graph_store')
def store(factory):return module().FundMemberGraphStore(factory)


def setup(factory,tmp_path,code='512660.SH',v=None):
    c=load(tmp_path,value() if v is None else v);doc='fund-'+code[:6];p=build_discovery_plan(c,doc,NOW-timedelta(seconds=3));s=FundContractStore(factory)
    key=s.reserve(p,NOW-timedelta(seconds=3));s.receive(key,mapping_body(code[:6],p['target']['sourceIndexNameHint'],resource='123456'),NOW-timedelta(seconds=2))
    s.replay(key,NOW-timedelta(seconds=1))
    proof=module().resolve_source_contract(factory,c,code,NOW)
    return c,build_fund_graph_intent(c,code,proof,NOW)


def csi_bytes(index='399967',day='20261022'):
    return compound_workbook(rows=[row(index=index,date=day),row(index=index,date=day,code='000001',exchange='深圳证券交易所',exchangeEnglish='Shenzhen Stock Exchange')])


def complete_csi(s,p):
    key=s.reserve(p,'csi-members',NOW+timedelta(seconds=1))
    s.receive(key,csi_bytes(p['indexCode']),NOW+timedelta(seconds=2));s.replay(key,NOW+timedelta(seconds=3));return key


def test_three_real_receipts_share_only_two_new_attempts_and_stay_fixed_next_day(database,tmp_path):
    _,f=database;c,p=setup(f,tmp_path);s=store(f)
    assert s.inspect(p,NOW)['status']=='new'
    with pytest.raises(ValueError):s.reserve(p,'fund-pcf',NOW)
    complete_csi(s,p)
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4))
    with f() as session:assert session.get(JobRun,key).details['plan']['document']['tradingDate']=='2026-10-22'
    s.receive(key,pcf_body(code='512660',day='20261022'),NOW+timedelta(seconds=5));s.replay(key,NOW+timedelta(seconds=6))
    state=s.inspect(p,NOW+timedelta(seconds=7))
    assert state['status']=='completed' and state['witnessReady']
    assert count(f,JobRun)==3 and count(f,SourcePayload)==3  # 1已存声明+2新叶，无伪造0请求槽。
    later=build_fund_graph_intent(c,'512660.SH',module().resolve_source_contract(f,c,'512660.SH',NOW+timedelta(days=1)),NOW+timedelta(days=1))
    assert s.inspect(later,NOW+timedelta(days=1))['intent']==p
    assert s.replay(key,NOW+timedelta(days=1))['readOnly']


@pytest.mark.parametrize('bad',['date','members'])
def test_wrong_pcf_date_or_members_preserves_raw_and_never_refetches(database,tmp_path,bad):
    _,f=database;_,p=setup(f,tmp_path);s=store(f);complete_csi(s,p)
    raw=pcf_body(code='512660',day='20261021') if bad=='date' else pcf_body(code='512660',day='20261022').replace(b'000001',b'600001')
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4));s.receive(key,raw,NOW+timedelta(seconds=5))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=6))
    state=s.inspect(p,NOW+timedelta(seconds=7));assert state['status']=='replay_pending' and not state['witnessReady']
    with pytest.raises(ValueError):s.reserve(p,'fund-pcf',NOW+timedelta(days=1))
    assert count(f,JobRun)==3 and count(f,SourcePayload)==3


def test_failed_graph_does_not_refund_or_block_another_fund(database,tmp_path):
    _,f=database;_,p=setup(f,tmp_path);_,other=setup(f,tmp_path,'515220.SH');s=store(f)
    key=s.reserve(p,'csi-members',NOW+timedelta(seconds=1));s.fail(key,'EXTENSION_AUTH_ERROR',NOW+timedelta(seconds=2))
    assert s.inspect(p,NOW+timedelta(days=1))['status']=='blocked'
    with pytest.raises(ValueError):s.reserve(p,'csi-members',NOW+timedelta(days=1))
    assert s.reserve(other,'csi-members',NOW+timedelta(seconds=3))!=key
    from app.macro.official_member_budget import daily_budget
    with f() as session:assert daily_budget(session,NOW+timedelta(seconds=4),4)['remaining']==0


def test_forged_but_well_shaped_metadata_must_match_database_raw(database,tmp_path):
    _,f=database;_,p=setup(f,tmp_path);s=store(f);bad=deepcopy(p)
    bad['sourceContract']['normalized']['downloadResourceKey']='999999'
    bad['sources']['pcfTemplate'].update(downloadResourceKey='999999',urlTemplate='https://m.gtfund.com/cochin/etf/download/999999/{yyyymmdd}')
    bad['fingerprint']=digest({k:v for k,v in bad.items() if k!='fingerprint'})
    assert validate_fund_graph_intent(bad)  # 纯结构合法仍不是DB证明。
    with pytest.raises(ValueError):s.reserve(bad,'csi-members',NOW+timedelta(seconds=1))
    assert count(f,JobRun)==1


def test_pcf_parent_receipt_is_rechecked_against_actual_csi_row(database,tmp_path):
    _,f=database;_,p=setup(f,tmp_path);s=store(f);complete_csi(s,p)
    key=s.reserve(p,'fund-pcf',NOW+timedelta(seconds=4))
    with f.begin() as session:
        r=session.get(JobRun,key);child=deepcopy(r.details['plan']);child['csiReceipt']['rawId']=999999
        child['fingerprint']=digest({k:v for k,v in child.items() if k!='fingerprint'});r.details={**r.details,'plan':child}
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=5))


def test_earlier_timestamp_cannot_refund_graph_shared_budget(database,tmp_path):
    _,f=database;_,p=setup(f,tmp_path);s=store(f);key=s.reserve(p,'csi-members',NOW+timedelta(seconds=1))
    with f.begin() as session:
        session.get(JobRun,key).started_at=NOW-timedelta(days=1);session.get(IngestionBatch,key).started_at=NOW-timedelta(days=1)
    with pytest.raises(ValueError):s.inspect(p,NOW+timedelta(seconds=2))
    from app.macro.official_member_budget import daily_budget
    with f() as session:
        with pytest.raises(ValueError):daily_budget(session,NOW+timedelta(seconds=2),4)


def test_unused_intent_expiry_and_saved_contract_future_visibility(database,tmp_path):
    _,f=database;c,p=setup(f,tmp_path);s=store(f)
    assert module().resolve_source_contract(f,c,'512660.SH',NOW-timedelta(seconds=2)) is None
    with pytest.raises(ValueError):s.reserve(p,'csi-members',NOW+timedelta(days=1))
    s.reserve(p,'csi-members',NOW+timedelta(seconds=1))
    changed=deepcopy(p);changed['limits']['maxHttpPerRun']=1
    changed['fingerprint']=digest({k:v for k,v in changed.items() if k!='fingerprint'})
    with pytest.raises(ValueError):s.inspect(changed,NOW+timedelta(seconds=2))
