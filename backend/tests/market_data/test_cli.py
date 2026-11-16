from backend.tests.support.configuration import assert_fixture_configuration_rejected
import importlib
import json
from contextlib import contextmanager

from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from backend.tests.market_data.test_repository import NOW, setup
from backend.tests.market_data.test_sync import job
from backend.tests.marts.test_content_runtime import prepare, state


def runtime(new_pg_factory, monkeypatch, *, seeded=False):
    assert importlib.util.find_spec('app.market.data.runtime') is not None, 'market runtime missing'
    module = importlib.import_module('app.market.data.runtime')
    _, factory, _ = setup(new_pg_factory)
    prepare(factory)
    if seeded:
        sync, client, _ = job(factory)
        try:
            sync.run(NOW)
        finally:
            client.close()
    @contextmanager
    def readonly(_url):
        yield factory
    monkeypatch.setattr(module, 'open_readonly_factory', readonly)
    monkeypatch.setattr(module, 'create_engine_and_session', lambda _: (factory.kw['bind'], factory))
    monkeypatch.setattr(factory.kw['bind'], 'dispose', lambda: None)
    return module, factory


def test_publish_with_dry_run_is_read_only(new_pg_factory, monkeypatch):
    module, factory = runtime(new_pg_factory, monkeypatch)
    before = state(factory)
    def forbidden(*args, **kwargs):
        raise AssertionError('client must not be created')
    monkeypatch.setattr(module, 'TushareClient', forbidden)
    report = module.run_market_update(Settings(data_backend='database', database_url='postgresql+psycopg://controlled.invalid/test'),
        now=NOW, dry_run=True, publish=True)
    assert report['status'] == 'planned' and report['requestCount'] == 0
    assert state(factory) == before


def test_missing_token_with_valid_completed_cache_needs_no_client(new_pg_factory, monkeypatch):
    module, factory = runtime(new_pg_factory, monkeypatch, seeded=True)
    def forbidden(*args, **kwargs):
        raise AssertionError('client must not be created')
    monkeypatch.setattr(module, 'TushareClient', forbidden)
    before = state(factory)
    settings = Settings(data_backend='database', database_url='postgresql+psycopg://controlled.invalid/test')
    collected = module.run_market_update(settings, now=NOW)
    assert collected['status'] == 'completed' and collected['requestCount'] == 0
    assert state(factory) == before
    published = module.run_market_update(settings, now=NOW, publish=True)
    assert published['publication']['status'] == 'completed' and published['requestCount'] == 0


def test_pending_without_token_is_configuration_error(new_pg_factory, monkeypatch):
    module, _ = runtime(new_pg_factory, monkeypatch)
    report = module.run_market_update(Settings(data_backend='database', database_url='postgresql+psycopg://controlled.invalid/test'), now=NOW)
    assert report['status'] == 'configuration_error' and report['code'] == 'MARKET_CREDENTIAL_REQUIRED'
    assert report['requestCount'] == 0


def test_invalid_config_fails_before_connection_or_client(new_pg_factory, monkeypatch):
    module, _ = runtime(new_pg_factory, monkeypatch)
    def invalid():
        raise ValueError('secret-config-value')
    def forbidden(*args, **kwargs):
        raise AssertionError('database must not be opened')
    monkeypatch.setattr(module.MarketDataCatalog, 'load', invalid)
    monkeypatch.setattr(module, 'create_engine_and_session', forbidden)
    monkeypatch.setattr(module, 'open_readonly_factory', forbidden)
    report = module.run_market_update(Settings(data_backend='database', database_url='postgresql+psycopg://controlled.invalid/test'), now=NOW)
    assert report['status'] == 'configuration_error' and 'secret-config-value' not in str(report)


def test_fixture_is_rejected_and_cli_is_registered(new_pg_factory, monkeypatch):
    module, _ = runtime(new_pg_factory, monkeypatch)
    assert_fixture_configuration_rejected(monkeypatch)
    assert module.run_market_update(Settings(database_url=None, database_read_url=None), now=NOW)['status'] == 'configuration_error'
    monkeypatch.setattr(Settings, 'from_env', lambda: Settings(database_url=None, database_read_url=None))
    result = CliRunner().invoke(app, ['sync-market-data', '--dry-run'])
    assert result.exit_code == 2
    assert json.loads(result.stdout)['status'] == 'configuration_error'
    assert CliRunner().invoke(app, ['sync-market-data', '--help']).exit_code == 0


def test_unknown_secret_argument_and_database_errors_are_redacted(new_pg_factory, monkeypatch):
    module, _ = runtime(new_pg_factory, monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError('postgresql://private-user:secret-pass@private-host/database')
    monkeypatch.setattr(module, 'create_engine_and_session', fail)
    report = module.run_market_update(Settings(data_backend='database', database_url='postgresql+psycopg://controlled.invalid/test', tushare_token='secret-pass'), now=NOW)
    assert report['status'] == 'failed'
    assert all(secret not in str(report) for secret in ('private-user','secret-pass','private-host'))
    result = CliRunner().invoke(app, ['sync-market-data', '--token', 'secret-pass'])
    assert result.exit_code == 2 and 'secret-pass' not in result.output
