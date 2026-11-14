from backend.tests.support.configuration import assert_fixture_configuration_rejected
from datetime import UTC, datetime
import importlib
import importlib.util
import json

from typer.testing import CliRunner
from sqlalchemy import text
import pytest

from app.cli.main import app
from app.config import Settings
from backend.tests.macro.test_enterprise_cli import database
from backend.tests.macro.test_rea_input_store import database as evidence_database


def module():
    assert importlib.util.find_spec('app.macro.jobs.rea_inputs_runtime'), 'REA runtime missing'
    return importlib.import_module('app.macro.jobs.rea_inputs_runtime')


def test_dry_run_reads_missing_or_existing_schema_without_any_database_write(database,monkeypatch):
    path,factory=database
    runtime=module()
    before=path.read_bytes()
    result=runtime.run_enterprise_rea_inputs(Settings.from_env(),dry_run=True,now=datetime(2024,8,1,tzinfo=UTC))
    assert result['status']=='preview' and result['schemaReady']
    assert result['requestCount']==0 and path.read_bytes()==before
    with factory.begin() as s:
        for table in ('enterprise_rea_supplement','enterprise_rea_response','enterprise_rea_attempt','enterprise_rea_plan'):
            s.execute(text('DROP TABLE '+table))
    before=path.read_bytes()
    result=runtime.run_enterprise_rea_inputs(Settings.from_env(),dry_run=True)
    assert not result['schemaReady'] and len(result['missingTables'])==4
    assert path.read_bytes()==before


def test_runtime_refuses_fixture_non_postgres_and_naive_time(database,monkeypatch):
    runtime=module()
    assert runtime.run_enterprise_rea_inputs(Settings.from_env())['errorCode']=='ENTERPRISE_POSTGRES_REQUIRED'
    with pytest.raises(ValueError,match='REA_AWARE_TIME_REQUIRED'):
        runtime.run_enterprise_rea_inputs(Settings.from_env(),dry_run=True,now=datetime(2024,8,1))
    assert_fixture_configuration_rejected(monkeypatch)


def test_runtime_lock_and_inside_lock_recapture_prevent_stale_source_execution(evidence_database,monkeypatch):
    runtime=module();_,_,_,engine,factory=evidence_database
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled/db',tushare_token='controlled')
    calls=[]
    class Lock:
        def try_acquire(self,key): calls.append('acquire');return True
        def release(self,key): calls.append('release')
    # 外部资源边界替身；实际schema/事务/恢复另由真实SQLite与登记PG覆盖。
    count=[0]
    def two_captures(*_):
        count[0]+=1;calls.append('capture')
        return None,None,None,{'schemaReady':count[0]==1,'missingTables':[], 'blockedReasons':[]}
    monkeypatch.setattr(runtime,'_capture',two_captures)
    monkeypatch.setattr(runtime,'create_engine_and_session',lambda _: (engine,factory))
    monkeypatch.setattr(runtime,'job_lock_for_engine',lambda _: Lock())
    monkeypatch.setattr(runtime,'execute_rea_inputs_batch',lambda *a,**kw: pytest.fail('stale capture must not execute'))
    result=runtime.run_enterprise_rea_inputs(settings)
    assert result['status']=='blocked' and result['errorCode']=='REA_SUPPLEMENT_SCHEMA_REQUIRED'
    assert calls==['capture','acquire','capture','release']


@pytest.mark.parametrize('case',['no-token','locked','executor-error'])
def test_runtime_refusals_and_lock_cleanup(evidence_database,monkeypatch,case):
    runtime=module();_,_,_,engine,factory=evidence_database
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://controlled/db',
        tushare_token=None if case=='no-token' else 'controlled')
    calls=[]
    monkeypatch.setattr(runtime,'_capture',lambda *_: (None,None,None,
        {'schemaReady':True,'missingTables':[],'blockedReasons':[]}))
    monkeypatch.setattr(runtime,'create_engine_and_session',lambda _: (engine,factory))
    class Lock:
        def try_acquire(self,key):
            assert key=='enterprise-rea-inputs'
            calls.append('acquire')
            return case!='locked'
        def release(self,key): calls.append('release')
    monkeypatch.setattr(runtime,'job_lock_for_engine',lambda _: Lock())
    def execute(*_,**__):
        assert case=='executor-error'
        raise RuntimeError('controlled executor failure')
    monkeypatch.setattr(runtime,'execute_rea_inputs_batch',execute)
    if case=='executor-error':
        with pytest.raises(RuntimeError,match='controlled executor failure'):
            runtime.run_enterprise_rea_inputs(settings)
        assert calls==['acquire','release']
    else:
        result=runtime.run_enterprise_rea_inputs(settings)
        assert result['requestCount']==0
        if case=='no-token':
            assert result['errorCode']=='ENTERPRISE_TUSHARE_TOKEN_REQUIRED' and calls==[]
        else:
            assert result['status']=='locked' and calls==['acquire']


def test_sync_and_probe_cli_are_readonly_on_dry_run_and_source_is_closed(database):
    path,_=database
    before=path.read_bytes()
    for args in (['sync-enterprise-rea-inputs','--dry-run'],['probe-enterprise-rea-inputs','--dry-run','--source','cashflow']):
        result=CliRunner().invoke(app,args)
        assert result.exit_code==0,result.stdout
        value=next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{') and 'requestCount' in line)
        assert value['readOnly'] and value['requestCount']==0
    result=CliRunner().invoke(app,['probe-enterprise-rea-inputs','--dry-run','--source','arbitrary'])
    assert result.exit_code==2 and 'REA_SOURCE_INVALID' in result.stdout
    assert path.read_bytes()==before


def test_cli_connection_error_is_sanitized(monkeypatch):
    monkeypatch.setenv('DANG_DATA_BACKEND','database')
    monkeypatch.setenv('DANG_DATABASE_URL','invalid://private:password@private-host/db')
    result=CliRunner().invoke(app,['sync-enterprise-rea-inputs','--dry-run'])
    assert result.exit_code==2 and 'REA_INPUT_RUN_FAILED' in result.stdout
    assert 'password' not in result.stdout+result.stderr and 'private-host' not in result.stdout+result.stderr
