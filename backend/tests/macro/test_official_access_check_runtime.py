from backend.tests.support.configuration import assert_fixture_configuration_rejected
from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace

import httpx
import pytest
import typer
from typer.testing import CliRunner

from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_official_access_check import prepared,CHECK
from app.db.models import JobRun


def module():
    try:return import_module('app.macro.jobs.official_access_check')
    except ModuleNotFoundError:pytest.fail('official access runtime not implemented')


@pytest.mark.parametrize('backend,url',[('fixture',None),('database','mysql://invalid/rejected')])
def test_runtime_requires_primary_postgres_before_any_connection(monkeypatch,backend,url):
    m=module();monkeypatch.setattr(m,'create_engine_and_session',lambda *_:pytest.fail('no connection'))
    if backend == 'fixture':
        assert_fixture_configuration_rejected(monkeypatch)
        return
    result=m.run_official_access_check(SimpleNamespace(data_backend=backend,database_url=url),'b6856806-e449-5e55-b42c-07fe5f89b324')
    assert result['status']=='blocked' and result['readOnly'] and result['supplierHttp']==0


def test_runtime_dry_run_uses_readonly_factory_and_no_write_adapter(database,monkeypatch):
    _,f=database;parent,c=prepared(f);m=module();opened=[]
    @contextmanager
    def readonly(url):opened.append(url);yield f
    monkeypatch.setattr(m,'open_readonly_factory',readonly)
    monkeypatch.setattr(m,'create_engine_and_session',lambda *_:pytest.fail('write adapter'))
    monkeypatch.setattr(m,'load_extension_config',lambda:c)
    result=m.run_official_access_check(SimpleNamespace(data_backend='database',database_url='postgresql+psycopg://unused'),parent,dry_run=True)
    assert result['status']=='preview' and result['supplierHttp']==0 and len(opened)==1 and count(f,JobRun)==1


def test_locked_runtime_never_sends_or_reserves(database,monkeypatch):
    _,f=database;parent,c=prepared(f);m=module();disposed=[]
    monkeypatch.setattr(m,'create_engine_and_session',lambda *_:(SimpleNamespace(dispose=lambda:disposed.append(True)),f))
    monkeypatch.setattr(m,'job_lock_for_engine',lambda _:SimpleNamespace(try_acquire=lambda _:False))
    monkeypatch.setattr(m,'load_extension_config',lambda:c)
    result=m.run_official_access_check(SimpleNamespace(data_backend='database',database_url='postgresql+psycopg://unused'),parent,
        transport=httpx.MockTransport(lambda _:pytest.fail('locked')))
    assert result['status']=='locked' and result['supplierHttp']==0 and count(f,JobRun)==1 and disposed==[True]


def test_cli_accepts_only_explicit_parent_and_dry_run(monkeypatch):
    m=module();calls=[]
    monkeypatch.setattr(m,'run_official_access_check',lambda settings,parent,**kw:(calls.append((parent,kw)) or dict(status='preview',readOnly=True,supplierHttp=0)))
    cli=import_module('app.cli.macro_extensions');monkeypatch.setattr(cli.Settings,'from_env',lambda:object())
    app=typer.Typer();cli.register_macro_extension_commands(app);runner=CliRunner()
    result=runner.invoke(app,['check-official-macro-access','--failed-run-id','b6856806-e449-5e55-b42c-07fe5f89b324','--dry-run'])
    assert result.exit_code==0 and calls[0][1]==dict(dry_run=True)
    help_result=runner.invoke(app,['check-official-macro-access','--help'])
    assert '--force' not in help_result.output and '--url' not in help_result.output and '--now' not in help_result.output


def test_cli_invalid_parent_and_configuration_errors_never_echo_values(monkeypatch):
    m=module();cli=import_module('app.cli.macro_extensions');calls=[]
    def settings():calls.append(True);raise ValueError('sensitive-data')
    monkeypatch.setattr(cli.Settings,'from_env',settings)
    app=typer.Typer();cli.register_macro_extension_commands(app);runner=CliRunner()
    result=runner.invoke(app,['check-official-macro-access','--failed-run-id','invalid'])
    assert result.exit_code==2 and calls==[] and 'sensitive-data' not in result.output
    result=runner.invoke(app,['check-official-macro-access','--failed-run-id','b6856806-e449-5e55-b42c-07fe5f89b324'])
    assert result.exit_code==2 and 'sensitive-data' not in result.output
