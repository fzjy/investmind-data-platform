from backend.tests.support.configuration import assert_fixture_configuration_rejected
import json

import pytest
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from backend.tests.macro.test_enterprise_cli import database


def test_preview_reads_actual_core_without_token_and_preserves_file(database,monkeypatch):
    path,_=database
    monkeypatch.delenv('DANG_TUSHARE_TOKEN',raising=False)
    before=path.read_bytes()
    result=CliRunner().invoke(app,['preview-enterprise-receivables'])
    assert result.exit_code==0,result.stdout
    data=next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{') and 'planFingerprint' in line)
    assert data['status']=='preview' and data['readOnly'] is True
    assert data['physicalHttpExecuted']==0 and data['logicalRequestCount']==2
    assert data['poolCount']==50 and data['physicalBudgetEnforced'] is False
    assert path.read_bytes()==before


@pytest.mark.parametrize('mode',['custom','all'])
def test_preview_enterprise_pool_is_independent_from_stock_sync_mode(database,monkeypatch,mode):
    monkeypatch.setenv('DANG_SYNC_UNIVERSE',mode)
    result=CliRunner().invoke(app,['preview-enterprise-receivables'])
    assert result.exit_code==0,result.stdout
    assert '"poolCount": 50' in result.stdout


def test_preview_fixture_mode_refused(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)


def test_preview_connection_error_is_sanitized(monkeypatch):
    monkeypatch.setenv('DANG_DATA_BACKEND','database')
    monkeypatch.setenv('DANG_DATABASE_URL','invalid://private:password@private-host/db')
    result=CliRunner().invoke(app,['preview-enterprise-receivables'])
    assert result.exit_code==2 and 'ENTERPRISE_PREVIEW_FAILED' in result.stdout
    assert 'password' not in result.stdout+result.stderr and 'private-host' not in result.stdout+result.stderr


def test_preview_quarter_configuration_is_bounded(monkeypatch):
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_SUPPLEMENT_QUARTERS','3')
    assert getattr(Settings.from_env(),'macro_enterprise_supplement_quarters',None)==3
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_SUPPLEMENT_QUARTERS','9')
    with pytest.raises(ValueError): Settings.from_env()
