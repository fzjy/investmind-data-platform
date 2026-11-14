from copy import deepcopy
from datetime import timedelta
from importlib import import_module

import httpx

from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_catalog_member_policy import NOW,value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_official_members import row
from backend.tests.macro.official_xls_fixture import compound_workbook


def setup(tmp_path,monkeypatch,limit=4):
    v=value();v['catalogEvidence']['collectionEnabled']=True;v['officialEvidence']['maxHttpPerRun']=limit
    catalog=load(tmp_path,v);module=import_module('app.macro.jobs.catalog_member_runtime')
    monkeypatch.setattr(module,'load_enterprise_target_catalog',lambda:catalog)
    return catalog,module


def handler(factory,calls,fail=None):
    def call(request):
        calls.append(str(request.url));assert count(factory,JobRun)==len(calls)
        if fail and fail in str(request.url):return response(403,content=b'not logged')
        code='000905' if '000905' in str(request.url) else '000510'
        rows=[row(index=code,indexName='中证500' if code=='000905' else '中证A500'),
              row(index=code,indexName='中证500' if code=='000905' else '中证A500',code='000001',exchange='深圳证券交易所',exchangeEnglish='Shenzhen Stock Exchange')]
        return response(200,content=compound_workbook(rows=rows))
    return call


def execute(f,c,m,h,at=NOW,**kwargs):
    return m.execute_catalog_members(f,c,clock=lambda:at,transport=httpx.MockTransport(h),**kwargs)


def test_normal_entry_keeps_pending_fund_and_next_day_finished_targets_zero_http(database,tmp_path,monkeypatch):
    _,f=database;c,m=setup(tmp_path,monkeypatch);calls=[];h=handler(f,calls)
    result=execute(f,c,m,h)
    assert result['status']=='partial' and result['supplierHttp']==2 and count(f,SourcePayload)==2
    assert [g['status'] for g in result['groups']]==['completed','completed','source_pending']
    assert not result['sourceCoverageReady'] and result['configuredTargetsCompleted']
    again=execute(f,c,m,h,at=NOW+timedelta(days=2))
    assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==2


def test_failure_target_does_not_block_other_and_never_retries_on_new_day(database,tmp_path,monkeypatch):
    _,f=database;c,m=setup(tmp_path,monkeypatch);calls=[];h=handler(f,calls,fail='000905')
    result=execute(f,c,m,h)
    assert result['supplierHttp']==2 and [g['status'] for g in result['groups']]==['blocked','completed','source_pending']
    assert count(f,SourcePayload)==1
    later=execute(f,c,m,h,at=NOW+timedelta(days=1))
    assert later['supplierHttp']==0 and len(calls)==2


def test_run_limit_is_shared_across_targets_not_copied_for_each(database,tmp_path,monkeypatch):
    _,f=database;c,m=setup(tmp_path,monkeypatch,limit=1);calls=[];h=handler(f,calls)
    first=execute(f,c,m,h)
    assert first['supplierHttp']==1 and first['groups'][1]['status']=='budget_pending'
    second=execute(f,c,m,h)
    assert second['supplierHttp']==1 and count(f,SourcePayload)==2


def test_disabled_and_live_config_change_never_reserve_or_send(database,tmp_path,monkeypatch):
    _,f=database;c,m=setup(tmp_path,monkeypatch);v=value();disabled=load(tmp_path,v)
    calls=[];h=handler(f,calls)
    assert execute(f,disabled,m,h)['status']=='disabled'
    monkeypatch.setattr(m,'load_enterprise_target_catalog',lambda:disabled)
    result=execute(f,c,m,h)
    assert result['supplierHttp']==0 and not calls and count(f,JobRun)==0


def test_bad_raw_replay_never_causes_diagnostic_http_or_blocks_other_target(database,tmp_path,monkeypatch):
    _,f=database;c,m=setup(tmp_path,monkeypatch);calls=[];valid=handler(f,calls)
    def h(request):
        if '000905' in str(request.url):calls.append(str(request.url));return response(200,content=b'bad workbook')
        return valid(request)
    first=execute(f,c,m,h)
    assert first['supplierHttp']==2 and [g['status'] for g in first['groups']]==['replay_pending','completed','source_pending']
    again=execute(f,c,m,h)
    assert again['supplierHttp']==0 and len(calls)==2 and count(f,SourcePayload)==2
