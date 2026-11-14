from backend.tests.support.configuration import assert_fixture_configuration_rejected
import json
from datetime import datetime

from typer.testing import CliRunner

from app.cli.main import app
from backend.tests.macro.test_enterprise_cli import database


def report(result):
    return next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{') and 'contractVersion' in line)


def test_rea_preview_cli_uses_real_readonly_core_and_never_mutates_database(database,monkeypatch):
    path,_=database
    from app.cli import macro_enterprise
    class ControlledDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2024,8,1,tzinfo=tz)
    monkeypatch.setattr(macro_enterprise,'datetime',ControlledDateTime)
    before=path.read_bytes()
    result=CliRunner().invoke(app,['preview-enterprise-rea-inputs'])
    assert result.exit_code==0,result.stdout
    value=report(result)
    assert value['contractVersion']=='enterprise-rea-inputs-preview-v1'
    assert value['poolCount']==50 and value['readOnly'] and value['physicalHttpExecuted']==0
    assert len(value['outputPeriods'])==8
    observed=next(d for d in value['decisions'] if d['symbol']=='000001.SZ' and d['table']=='balance' and d['period']=='2024-03-31')
    assert observed['reason']=='source-witness-missing'
    assert observed['fieldStates']['fix_assets']=='missing-field'
    assert observed['coreBinding']['annDate']=='2024-04-30'
    assert not value['persistenceImplemented'] and not value['physicalBudgetEnforced']
    assert path.read_bytes()==before


def test_rea_preview_fixture_mode_is_refused_without_collection(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)


def test_rea_preview_connection_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv('DANG_DATA_BACKEND','database')
    monkeypatch.setenv('DANG_DATABASE_URL','invalid://private:password@private-host/db')
    result=CliRunner().invoke(app,['preview-enterprise-rea-inputs'])
    assert result.exit_code==2
    assert 'ENTERPRISE_REA_INPUT_PREVIEW_FAILED' in result.stdout
    assert 'password' not in result.stdout+result.stderr
    assert 'private-host' not in result.stdout+result.stderr
