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
def test_cli_maps_batch_status_and_consumes_settings(monkeypatch, status, code):
    seen = []
    class Job:
        def run(self, **kwargs):
            seen.append(kwargs)
            return {'status': status, 'requestCount': 2, 'rowCount': 0}
    @contextmanager
    def runtime(settings):
        yield type('Runtime', (), {'job': Job(), 'releases': ()})()
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url='sqlite://', macro_backfill_request_limit=7, macro_backfill_end=date(2026, 8, 31)))
    monkeypatch.setattr(cli, 'open_macro_history_runtime', runtime, raising=False)
    result = CliRunner().invoke(cli.app, ['backfill-macro-history'])
    assert result.exit_code == code, result.output
    assert json.loads(result.stdout)['status'] == status
    assert len(result.stdout.strip().splitlines()) == 1
    assert seen[0]['request_limit'] == 7
    assert seen[0]['end'] == date(2026, 8, 31)


@pytest.mark.parametrize('command', ['backfill-macro-history', 'macro-history-status'])
def test_missing_database_is_setup_error_without_secrets(monkeypatch, command):
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url=None, database_read_url=None, fred_api_key='fred-private', tushare_token='tushare-private'))
    result = CliRunner().invoke(cli.app, [command])
    assert result.exit_code == 3
    assert json.loads(result.stdout)['status'] == 'failed'
    assert json.loads(result.stdout)['error']['code'] == 'NBS_HISTORY_DATABASE_REQUIRED'
    assert 'fred-private' not in result.output and 'tushare-private' not in result.output


@pytest.mark.parametrize('command,code', [
    ('backfill-macro-history', 'NBS_HISTORY_CHECKPOINT_INVALID'),
    ('backfill-macro-history', 'NBS_HISTORY_CONFIG_INVALID'),
    ('backfill-macro-history', 'NBS_HISTORY_CONFIG_MISMATCH'),
    ('backfill-macro-history', 'NBS_HISTORY_MANIFEST_INVALID'),
    ('backfill-macro-history', 'NBS_HISTORY_MANIFEST_CONFLICT'),
    ('macro-history-status', 'NBS_HISTORY_CHECKPOINT_INVALID'),
])
def test_cli_preserves_allowlisted_recovery_error_code(monkeypatch, command, code):
    def fail(*args, **kwargs):
        raise ValueError(code)
    @contextmanager
    def runtime(settings):
        yield type('Runtime', (), {'job': type('Job', (), {'run': fail})(), 'releases': ()})()
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url='sqlite://'))
    monkeypatch.setattr(cli, 'open_macro_history_runtime', runtime)
    monkeypatch.setattr(cli, 'read_macro_history_status', fail)
    result = CliRunner().invoke(cli.app, [command])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': code}}
    assert len(result.stdout.strip().splitlines()) == 1


@pytest.mark.parametrize('command,fallback', [
    ('backfill-macro-history', 'NBS_HISTORY_SETUP_UNAVAILABLE'),
    ('macro-history-status', 'NBS_HISTORY_STATUS_UNAVAILABLE'),
])
def test_unknown_exception_cannot_leak_or_spoof_recovery_code(monkeypatch, command, fallback):
    def fail():
        raise ValueError('NBS_HISTORY_CHECKPOINT_INVALID postgres://private-user:private-password@host/db')
    monkeypatch.setattr(cli.Settings, 'from_env', fail)
    result = CliRunner().invoke(cli.app, [command])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': fallback}}
    assert 'private-user' not in result.output and 'private-password' not in result.output


def test_status_command_does_not_construct_network_and_does_not_write_cursor(monkeypatch, tmp_path):
    url = f'sqlite+pysqlite:///{tmp_path / "status.sqlite"}'
    engine, factory = create_engine_and_session(url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url=url))
    monkeypatch.setattr(cli.httpx, 'Client', lambda *a, **k: pytest.fail('HTTP constructed by status'))
    result = CliRunner().invoke(cli.app, ['macro-history-status'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['status'] == 'not_started'
    with factory() as session:
        assert session.scalars(select(JobCursor)).all() == []
    engine.dispose()


def test_runtime_single_attempt_is_bounded_and_restores_cursor(tmp_path):
    from app.macro.jobs.macro_history_runtime import open_macro_history_runtime
    url = f'sqlite+pysqlite:///{tmp_path / "runtime.sqlite"}'
    engine, factory = create_engine_and_session(url)
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    manifest = tmp_path / 'empty.json'
    manifest.write_text('{"schemaVersion":"1.0","releases":[]}', encoding='utf-8')
    settings = Settings(database_url=url, macro_backfill_releases_file=manifest)
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503)
    from datetime import UTC, datetime
    with open_macro_history_runtime(settings, transport=httpx.MockTransport(handler)) as runtime:
        result = runtime.job.run(as_of=datetime(2026, 10, 1, tzinfo=UTC), history_years=1, end=date(2026, 8, 31), request_limit=2, releases=runtime.releases)
    assert len(calls) == 1
    assert result['requestCount'] == 1 and result['nextIndexPage'] == 0
    with factory() as session:
        assert len(session.scalars(select(JobCursor)).all()) == 1
        assert session.scalars(select(MacroObservation)).all() == []
    engine.dispose()
