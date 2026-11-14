from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from app.db.models import JobRun,MacroObservation
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_official_access_replay import ready


def module():
    try:return import_module('app.macro.jobs.official_access_replay')
    except ModuleNotFoundError:pytest.fail('official zero-HTTP runtime not implemented')


def settings():return SimpleNamespace(data_backend='database',database_url='postgresql+psycopg://unused')


def test_preview_uses_only_readonly_factory_and_produces_no_record(database,monkeypatch):
    _,f=database;parent,c,_=ready(f);m=module();opened=[]
    @contextmanager
    def readonly(_):opened.append(True);yield f
    monkeypatch.setattr(m,'open_readonly_factory',readonly)
    monkeypatch.setattr(m,'load_extension_config',lambda:c)
    monkeypatch.setattr(m,'create_engine_and_session',lambda *_:pytest.fail('write adapter in dry run'))
    result=m.run_official_access_replay(settings(),parent,dry_run=True)
    assert result['status']=='preview' and result['supplierHttp']==0 and opened==[True]
    assert count(f,JobRun)==2 and count(f,MacroObservation)==0


def test_actual_runtime_has_zero_http_and_reentry_is_immutable(database,monkeypatch):
    _,f=database;parent,c,_=ready(f);m=module();disposed=[];released=[]
    monkeypatch.setattr(m,'load_extension_config',lambda:c)
    monkeypatch.setattr(m,'create_engine_and_session',lambda *_:(SimpleNamespace(dispose=lambda:disposed.append(True)),f))
    monkeypatch.setattr(m,'job_lock_for_engine',lambda _:SimpleNamespace(try_acquire=lambda _:True,release=lambda _:released.append(True)))
    first=m.run_official_access_replay(settings(),parent)
    again=m.run_official_access_replay(settings(),parent)
    assert first['status']==again['status']=='completed' and first['insertedRows']==1 and again['insertedRows']==0
    assert first['supplierHttp']==again['supplierHttp']==0 and first['recoveryCompleted'] and again['readOnly']
    assert count(f,JobRun)==3 and count(f,MacroObservation)==1 and len(disposed)==len(released)==2


def test_unavailable_lock_preserves_source_and_no_core(database,monkeypatch):
    _,f=database;parent,c,_=ready(f);m=module()
    monkeypatch.setattr(m,'load_extension_config',lambda:c)
    monkeypatch.setattr(m,'create_engine_and_session',lambda *_:(SimpleNamespace(dispose=lambda:None),f))
    monkeypatch.setattr(m,'job_lock_for_engine',lambda _:SimpleNamespace(try_acquire=lambda _:False))
    result=m.run_official_access_replay(settings(),parent)
    assert result['status']=='locked' and result['supplierHttp']==0 and count(f,JobRun)==2


def test_cli_is_only_parent_and_dry_run_and_does_not_echo_errors(monkeypatch):
    m=module();cli=import_module('app.cli.macro_extensions');calls=[]
    monkeypatch.setattr(m,'run_official_access_replay',lambda s,p,**kw:(calls.append(kw) or dict(status='preview',supplierHttp=0,readOnly=True)))
    monkeypatch.setattr(cli.Settings,'from_env',lambda:object())
    app=typer.Typer();cli.register_macro_extension_commands(app);runner=CliRunner()
    result=runner.invoke(app,['replay-official-access-check','--failed-run-id','b6856806-e449-5e55-b42c-07fe5f89b324','--dry-run'])
    assert result.exit_code==0 and calls==[dict(dry_run=True)]
    help_result=runner.invoke(app,['replay-official-access-check','--help'])
    assert '--force' not in help_result.output and '--url' not in help_result.output and '--now' not in help_result.output
