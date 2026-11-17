from backend.tests.support.pg_snapshot import database_snapshot
from backend.tests.support.configuration import assert_fixture_configuration_rejected
"""只读PostgreSQL实证、真实命令及异常脱敏。"""
from datetime import date, datetime, timezone
import json

import pytest
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from app.db.models.core import MacroObservation
from app.db.session import create_engine_and_session
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.preview import preview_macro_extensions


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path, monkeypatch, pg_url):
    from app.macro.extensions import config as extension_config
    from backend.tests.macro.test_extension_plan import config as controlled_config
    controlled_config(tmp_path)
    monkeypatch.setattr(extension_config, 'DEFAULT_EXTENSION_CONFIG_FILE', tmp_path / 'extensions.json')
    engine, factory = create_engine_and_session(pg_url)
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE SCHEMA core')
        MacroObservation.__table__.create(connection)
    monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
    monkeypatch.setenv('DANG_DATABASE_URL', pg_url)
    monkeypatch.setenv('DANG_DATABASE_READ_URL', pg_url)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def row(**changes):
    return MacroObservation(**{'source': 'tushare', 'series_id': 'fx.usdcnh',
        'name': 'offshore', 'region': 'CN', 'frequency': 'daily', 'unit': 'CNH/USD',
        'seasonal_adjustment': 'NSA', 'observed_at': date(2026, 10, 21),
        'released_at': datetime(2026, 10, 22, 0, tzinfo=timezone.utc),
        'revision': 'r1', 'value': '7.1', 'source_url': 'https://tushare.pro/document/2?doc_id=179',
        **changes})


def output(result):
    return next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))


def test_empty_preview_and_real_cli_are_readonly_and_not_executors(database):
    db_engine, _ = database
    before = database_snapshot(db_engine)
    result = preview_macro_extensions(Settings.from_env(), now=NOW)
    assert result['readOnly'] is True and result['supplierHttp'] == 0
    # C2现已实现执行能力，但只读预览不得冒称已执行/预留。
    assert result['executorImplemented'] is True and result['physicalBudgetImplemented'] is True
    assert all(result[key] is False for key in ['physicalBudgetEnforced', 'publicationRequested', 'planPersisted'])
    assert all(group['inventory']['rawRows'] == 0 for group in result['groups'])
    assert all(group['status'] == 'source_pending' for group in result['groups'])
    assert all(group['collectionEnabled'] is False for group in result['groups'])
    cli = CliRunner().invoke(app, ['preview-macro-extensions'])
    assert cli.exit_code == 0, cli.stdout
    assert output(cli)['supplierHttp'] == 0
    assert datetime.fromisoformat(output(cli)['checkedAt']).date() == datetime.now(timezone.utc).date()
    assert database_snapshot(db_engine) == before


def test_invalid_future_and_unrelated_inventory_never_count_as_usable(database):
    db_engine, factory = database
    with factory.begin() as session:
        session.add_all([row(), row(source='unknown'), row(revision='r2', unit='CNY/USD'),
            row(revision='r3', frequency='monthly'),
            row(revision='r4', released_at=datetime(2026, 10, 23, tzinfo=timezone.utc)),
            row(revision='r5', observed_at=date(2026, 10, 22)),
            row(revision='r6', value='-2'),
            row(series_id='cn.gdp.nominal_cumulative', frequency='quarterly', unit='亿元',
                observed_at=date(2026, 7, 2), source_url='https://tushare.pro/document/2?doc_id=227'),
            row(series_id='not-a-target')])
    before = database_snapshot(db_engine)
    result = preview_macro_extensions(Settings.from_env(), now=NOW)
    assert not result['schemaReady'] and 'ops.job_run' in result['problems']
    fx, gdp = result['groups']
    assert fx['inventory'] == {'rawRows': 7, 'validVisibleRows': 1,
        'invalidRows': 4, 'notVisibleRows': 2, 'distinctPeriods': 1}
    assert gdp['inventory']['invalidRows'] == 1
    assert fx['status'] == 'inventory_only' and fx['readyForPublication'] is False
    assert database_snapshot(db_engine) == before


def test_bad_config_fails_before_opening_database(database, tmp_path, monkeypatch):
    from app.macro.extensions import config, preview
    file = tmp_path / 'bad.json'
    file.write_text('{"private":"do-not-echo"}', encoding='utf-8')
    monkeypatch.setattr(config, 'DEFAULT_EXTENSION_CONFIG_FILE', file)
    def forbidden(*args, **kwargs):
        pytest.fail('invalid config opened database')
    monkeypatch.setattr(preview, 'open_readonly_factory', forbidden)
    with pytest.raises(ExtensionError): preview_macro_extensions(Settings.from_env(), now=NOW)
    result = CliRunner().invoke(app, ['preview-macro-extensions'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'MACRO_EXTENSION_PREVIEW_FAILED'}
    assert 'do-not-echo' not in result.stdout + result.stderr


def test_fixture_is_rejected_and_db_errors_are_sanitized(database, monkeypatch):
    from app.macro.extensions import preview
    assert_fixture_configuration_rejected(monkeypatch)
    monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
    def fail(*args, **kwargs): raise ValueError('password-do-not-echo')
    monkeypatch.setattr(preview, 'open_readonly_factory', fail)
    result = CliRunner().invoke(app, ['preview-macro-extensions'])
    assert result.exit_code == 2
    assert output(result)['errorCode'] == 'MACRO_EXTENSION_PREVIEW_FAILED'
    assert 'password-do-not-echo' not in result.stdout + result.stderr


@pytest.mark.parametrize('option', ['--force', '--as-of', '--publish', '--source', '--start-date'])
def test_no_write_or_simulated_clock_cli_options(option):
    result = CliRunner().invoke(app, ['preview-macro-extensions', option])
    assert result.exit_code == 2
    assert CliRunner().invoke(app, ['preview-macro-extensions', '--help']).exit_code == 0
