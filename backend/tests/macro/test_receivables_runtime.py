from backend.tests.support.configuration import assert_fixture_configuration_rejected
import json
from datetime import date

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from app.cli.main import app
from backend.tests.macro.test_enterprise_cli import database


def invoke(*args):
    return CliRunner().invoke(app,['sync-enterprise-receivables',*args])


def body(result):
    return next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))


def test_unmigrated_dry_run_has_no_token_no_writes_or_automatic_tables(database,monkeypatch):
    path,factory=database
    monkeypatch.delenv('DANG_TUSHARE_TOKEN',raising=False)
    with factory.begin() as session:
        for name in ('enterprise_receivable_supplement','enterprise_receivable_response','enterprise_receivable_attempt'):
            session.execute(text(f'DROP TABLE {name}'))
    before=path.read_bytes()
    result=invoke('--dry-run')
    assert result.exit_code==0,result.stdout
    data=body(result)
    assert data['status']=='preview' and data['schemaReady'] is False and len(data['missingTables'])==3
    assert data['poolCount']==50 and data['pendingCount']==2 and data['requestCount']==0
    assert data['readOnly'] is True and path.read_bytes()==before


def test_existing_supplement_schema_dry_run_is_still_byte_identical(database):
    path,_=database;before=path.read_bytes()
    result=invoke('--dry-run')
    assert result.exit_code==0,result.stdout
    data=body(result)
    assert data['schemaReady'] is True and data['pendingCount']==2 and data['matchedCount']==0
    assert path.read_bytes()==before


def test_write_entry_refuses_sqlite_without_changing_anything(database):
    path,_=database;before=path.read_bytes()
    result=invoke()
    assert result.exit_code==2 and body(result)['errorCode']=='ENTERPRISE_POSTGRES_REQUIRED'
    assert path.read_bytes()==before


def test_fixture_mode_is_not_accepted_for_field_collection(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)


def test_bad_connection_does_not_expose_dsn_or_secret(monkeypatch):
    monkeypatch.setenv('DANG_DATA_BACKEND','database')
    monkeypatch.setenv('DANG_DATABASE_URL','invalid://private:password@private-host/db')
    result=invoke('--dry-run')
    assert result.exit_code==2 and body(result)['errorCode']=='ENTERPRISE_RECEIVABLE_RUN_FAILED'
    assert 'password' not in result.stdout+result.stderr and 'private-host' not in result.stdout+result.stderr


def test_bad_timezone_returns_machine_error_without_traceback(database,monkeypatch):
    monkeypatch.setenv('DANG_SYNC_TIMEZONE','Asia/Shanghia')
    result=invoke('--dry-run')
    assert result.exit_code==2 and body(result)['errorCode']=='ENTERPRISE_RECEIVABLE_RUN_FAILED'
    assert 'Traceback' not in result.stdout+result.stderr


@pytest.fixture
def completed_database(database):
    from sqlalchemy import select
    from app.db.models import BalanceSheet
    from app.macro.jobs.receivables_batch import execute_receivables_batch
    from app.macro.enterprise import EnterprisePolicy
    from app.macro.enterprise_repository import load_enterprise_inputs
    from app.stocks.universes import UniverseResolver
    from backend.tests.macro.receivables_samples import full_inputs
    from backend.tests.macro.test_receivables_batch import transport_for
    path,factory=database
    witness=next(r.values for r in full_inputs().financials if r.table=='balance')
    with factory.begin() as session:
        for row in session.scalars(select(BalanceSheet)):
            row.data={**witness,**row.data}
    data=load_enterprise_inputs(factory,UniverseResolver().resolve('representative-50'))
    calls=[]
    completed=execute_receivables_batch(factory,data,date(2024,5,31),EnterprisePolicy(min_samples=1,min_coverage=1),
        quarters=8,request_limit=100,page_size=5000,token='controlled-token',transport=transport_for(data,calls))
    assert completed['matchedCount']==2 and len(calls)==2
    return path,factory


def test_populated_dry_run_reuses_verified_sql_evidence_without_writing(completed_database):
    path,_=completed_database
    before=path.read_bytes()
    result=invoke('--dry-run')
    assert result.exit_code==0,result.stdout
    status=body(result)
    assert status['matchedCount']==2 and status['pendingCount']==0 and status['processingCompleted'] is True
    assert status['requestCount']==0 and path.read_bytes()==before


@pytest.mark.parametrize('condition,code',[
    ('policy','ENTERPRISE_PAGE_POLICY_CHANGED'),('corrupt','ENTERPRISE_EVIDENCE_INVALID')])
def test_public_dry_run_keeps_specific_evidence_blocking_code(completed_database,monkeypatch,condition,code):
    from sqlalchemy import select
    from app.db.models import ReceivableResponse
    path,factory=completed_database
    if condition=='policy': monkeypatch.setenv('DANG_TUSHARE_PAGE_SIZE','1')
    else:
        with factory.begin() as session:
            session.scalar(select(ReceivableResponse)).response_hash='0'*64
    before=path.read_bytes()
    result=invoke('--dry-run')
    assert result.exit_code==2
    data=body(result)
    assert data['status']=='blocked' and data['errorCode']==code
    assert data['requestCount']==0 and data['readOnly'] is True and path.read_bytes()==before
