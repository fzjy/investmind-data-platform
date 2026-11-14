from contextlib import contextmanager
from datetime import date
import json

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli import main as cli
from app.config import Settings
from app.db.base import Base, ExternalBase
from app.db.models import JobCursor, MacroObservation
from app.db.session import create_engine_and_session


@pytest.mark.parametrize('status,code', [('completed', 0), ('partial', 4), ('failed', 4), ('locked', 5)])
def test_cli_status_and_file_configuration(monkeypatch, status, code):
    seen = []
    class Job:
        def run(self, **kwargs):
            seen.append(kwargs)
            return {'status': status, 'requestCount': 1, 'rowCount': 0}
    @contextmanager
    def runtime(settings):
        yield type('Runtime', (), {'job': Job()})()
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url='sqlite://',
        macro_history_years=10, macro_backfill_end=date(2026, 9, 30), macro_backfill_request_limit=12))
    monkeypatch.setattr(cli, 'open_dr007_history_runtime', runtime, raising=False)
    result = CliRunner().invoke(cli.app, ['backfill-dr007-history'])
    assert result.exit_code == code, result.output
    assert json.loads(result.stdout)['status'] == status
    assert len(result.stdout.strip().splitlines()) == 1
    assert seen[0]['history_years'] == 10 and seen[0]['end'] == date(2026, 9, 30)
    assert seen[0]['request_limit'] == 12


@pytest.mark.parametrize('command', ['backfill-dr007-history', 'dr007-history-status'])
def test_missing_database_does_not_print_secrets(monkeypatch, command):
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url=None, database_read_url=None, fred_api_key='private-key'))
    result = CliRunner().invoke(cli.app, [command])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': 'DR007_HISTORY_DATABASE_REQUIRED'}}
    assert 'private-key' not in result.output


@pytest.mark.parametrize('command,fallback', [('backfill-dr007-history', 'DR007_HISTORY_SETUP_UNAVAILABLE'),
    ('dr007-history-status', 'DR007_HISTORY_STATUS_UNAVAILABLE')])
def test_exception_text_cannot_spoof_error_or_leak_secret(monkeypatch, command, fallback):
    def fail():
        raise ValueError('DR007_HISTORY_CHECKPOINT_INVALID postgres://private:password@host/db')
    monkeypatch.setattr(cli.Settings, 'from_env', fail)
    result = CliRunner().invoke(cli.app, [command])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': fallback}}
    assert 'password' not in result.output


@pytest.mark.parametrize('code', ['DR007_HISTORY_CONFIG_INVALID', 'DR007_HISTORY_CONFIG_MISMATCH',
    'DR007_HISTORY_SOURCE_INVALID', 'DR007_HISTORY_CHECKPOINT_INVALID'])
def test_stable_recovery_errors_are_preserved(monkeypatch, code):
    @contextmanager
    def fail(settings):
        raise ValueError(code)
        yield
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings())
    monkeypatch.setattr(cli, 'open_dr007_history_runtime', fail, raising=False)
    result = CliRunner().invoke(cli.app, ['backfill-dr007-history'])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': code}}


def test_read_only_status_does_not_construct_http_or_write(monkeypatch, tmp_path):
    url = f'sqlite+pysqlite:///{tmp_path / "status.sqlite"}'
    engine, factory = create_engine_and_session(url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url=url))
    monkeypatch.setattr(httpx, 'Client', lambda *a, **k: pytest.fail('status constructed HTTP'))
    result = CliRunner().invoke(cli.app, ['dr007-history-status'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['status'] == 'not_started'
    with factory() as session:
        assert session.scalars(select(JobCursor)).all() == []
    engine.dispose()


def test_runtime_503_has_single_attempt(tmp_path):
    import importlib.util
    assert importlib.util.find_spec('app.macro.jobs.dr007_history_runtime') is not None
    from app.macro.jobs.dr007_history_runtime import open_dr007_history_runtime, read_dr007_history_status
    from backend.tests.jobs.test_dr007_history import NOW
    url = f'sqlite+pysqlite:///{tmp_path / "runtime.sqlite"}'
    engine, factory = create_engine_and_session(url)
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    settings = Settings(database_url=url, tushare_token='controlled-token')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    with open_dr007_history_runtime(settings, transport=httpx.MockTransport(handler)) as runtime:
        runtime.job.clock = lambda: NOW
        result = runtime.job.run(as_of=NOW, history_years=1, end=None, request_limit=1)
    assert len(calls) == 1 and result['requestCount'] == 1
    assert result['completedMonths'] == 0
    assert read_dr007_history_status(settings)['pendingMonths'] == 12
    with factory() as session:
        assert session.scalars(select(MacroObservation)).all() == []
    engine.dispose()
