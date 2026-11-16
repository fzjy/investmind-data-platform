from contextlib import contextmanager
import json

import pytest
from typer.testing import CliRunner

from app.cli import main as cli
from app.config import Settings
from app.macro.jobs.macro_sync import MacroSyncReport
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


@pytest.mark.parametrize('status,code', [('completed', 0), ('partial', 4), ('failed', 4), ('locked', 5)])
def test_scoped_command_uses_root_configuration_and_never_publishes(monkeypatch, status, code):
    seen = []
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    class Job:
        def run(self, options):
            seen.append(options)
            return MacroSyncReport(status, 'run', 'key', 1, options.as_of, options.catalog_version)
    @contextmanager
    def runtime(settings):
        assert settings.macro_history_years == 10
        yield type('Runtime', (), {'job': Job(), 'catalog': catalog})()
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: Settings(database_url='postgresql+psycopg://controlled.invalid/test', macro_history_years=10))
    monkeypatch.setattr(cli, 'open_govbond_sync_runtime', runtime, raising=False)
    result = CliRunner().invoke(cli.app, ['sync-govbond'])
    assert result.exit_code == code, result.output
    assert json.loads(result.stdout)['status'] == status
    assert seen[0].series_ids == ('cn.rate.govbond_10y',) and seen[0].publish is False
    assert seen[0].force and seen[0].mode == 'live'


@pytest.mark.parametrize('text,expected', [
    ('MACRO_HISTORY_CATALOG_UNAVAILABLE', 'MACRO_HISTORY_CATALOG_UNAVAILABLE'),
    ('GOVBOND_HISTORY_INCOMPLETE', 'GOVBOND_HISTORY_INCOMPLETE'),
    ('GOVBOND_HISTORY_INCOMPLETE postgres://user:secret@host/db', 'GOVBOND_SYNC_SETUP_UNAVAILABLE'),
])
def test_scoped_command_does_not_leak_exception_text(monkeypatch, text, expected):
    def fail():
        raise ValueError(text)
    monkeypatch.setattr(cli.Settings, 'from_env', fail)
    result = CliRunner().invoke(cli.app, ['sync-govbond'])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'status': 'failed', 'error': {'code': expected}}
    assert 'secret' not in result.output
