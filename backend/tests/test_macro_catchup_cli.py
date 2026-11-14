import json

import pytest
from typer.testing import CliRunner

from app.cli import main
from backend.tests.jobs.test_macro_catchup import database, controlled_groups, NOW


def test_cli_disabled_uses_root_settings_without_time_or_force_flags(database,monkeypatch):
    settings,factory,engine=database
    monkeypatch.setattr(main.Settings,'from_env',lambda:settings.model_copy(update={'macro_sync_enabled':False}))
    result=CliRunner().invoke(main.app,['catchup-macro'])
    assert result.exit_code==0, result.output
    assert json.loads(result.stdout)['status']=='disabled'


def test_cli_failure_output_is_safe(monkeypatch):
    def fail(): raise RuntimeError('private-token-and-password')
    monkeypatch.setattr(main.Settings,'from_env',fail)
    result=CliRunner().invoke(main.app,['catchup-macro'])
    assert result.exit_code==3
    assert json.loads(result.stdout)['error']['code']=='MACRO_CATCHUP_UNAVAILABLE'
    assert 'private' not in result.output


@pytest.mark.parametrize('flag',['--as-of','--force'])
def test_cli_has_no_clock_forgery_or_implicit_replay_override(flag):
    result=CliRunner().invoke(main.app,['catchup-macro',flag,'2026-10-11'])
    assert result.exit_code==2
