from backend.tests.support.pg_snapshot import database_snapshot
from backend.tests.support.configuration import assert_fixture_configuration_rejected
"""命令必须走真实配置/只读库存，不泄露异常或新增采集入口。"""
from datetime import datetime
import json
import re

import pytest
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from app.cli.main import app
from backend.tests.macro.test_enterprise_cli import database


def output(result):
    return next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))


def test_cli_reads_actual_postgres_and_preserves_database_and_uses_real_date(database):
    from app.config import Settings
    from zoneinfo import ZoneInfo
    db_engine, _ = database
    today = datetime.now(ZoneInfo(Settings.from_env().sync_timezone)).date().isoformat()
    before = database_snapshot(db_engine)
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 0, result.stdout
    value = output(result)
    assert value['asOf'] == today
    assert value['status'] == 'requirements_pending' and value['pool']['size'] == 50
    assert len(value['targets']) == 14 and value['targets'][2]['code'] == '000510.SH'
    assert value['inventory']['legacyConstituentRows'] == 0
    assert value['membershipReady'] is False and value['supplierHttp'] == 0
    assert database_snapshot(db_engine) == before


def test_cli_uses_custom_sixty_one_members_without_expansion(database, tmp_path, monkeypatch):
    db_engine, _ = database
    pool = tmp_path / 'custom.json'
    pool.write_text(json.dumps([f'{i:06}.SZ' for i in range(1, 62)]), encoding='utf-8')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE', 'custom')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE_FILE', str(pool))
    before = database_snapshot(db_engine)
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 0, result.stdout
    assert output(result)['pool']['size'] == 61 and output(result)['pool']['profile'] == 'custom'
    assert database_snapshot(db_engine) == before


def test_cli_empty_custom_pool_is_safe_failure_not_whole_market(database, tmp_path, monkeypatch):
    db_engine, _ = database
    pool = tmp_path / 'empty.json'
    pool.write_text('[]', encoding='utf-8')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE', 'custom')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE_FILE', str(pool))
    before = database_snapshot(db_engine)
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'ENTERPRISE_TARGET_PREVIEW_FAILED'}
    assert database_snapshot(db_engine) == before


def test_fixture_mode_is_refused_before_connection(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)


def test_real_settings_missing_database_url_is_classified_before_connection(monkeypatch):
    monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
    monkeypatch.delenv('DANG_DATABASE_URL', raising=False)
    monkeypatch.delenv('DANG_DATABASE_READ_URL', raising=False)
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'ENTERPRISE_DATABASE_REQUIRED'}


@pytest.mark.parametrize('cause', ['catalog', 'sql', 'timezone', 'driver', 'validation'])
def test_all_expected_failures_are_sanitized(database, monkeypatch, cause):
    from app.macro import enterprise_target_catalog, enterprise_target_repository
    secret = 'private-password-host-do-not-echo'
    if cause == 'catalog':
        def fail(*args, **kwargs):
            raise ValueError(secret)
        monkeypatch.setattr(enterprise_target_catalog, 'load_enterprise_target_catalog', fail)
    elif cause == 'sql':
        def fail(*args, **kwargs):
            raise SQLAlchemyError(secret)
        monkeypatch.setattr(enterprise_target_repository, 'load_target_evidence_inventory', fail)
    elif cause == 'timezone':
        monkeypatch.setenv('DANG_SYNC_TIMEZONE', secret)
    elif cause == 'validation':
        monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_SAMPLES', secret)
    else:
        monkeypatch.setenv('DANG_DATABASE_URL', f'invalid://{secret}@private/db')
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'ENTERPRISE_TARGET_PREVIEW_FAILED'}
    assert secret not in result.stdout + result.stderr


def test_bad_actual_catalog_file_is_sanitized(database, tmp_path, monkeypatch):
    from app.macro import enterprise_target_catalog
    file = tmp_path / 'broken.json'
    file.write_text('{"secret":"do-not-print"', encoding='utf-8')
    monkeypatch.setattr(enterprise_target_catalog, 'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE', file)
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 2
    assert output(result)['errorCode'] == 'ENTERPRISE_TARGET_PREVIEW_FAILED'
    assert 'do-not-print' not in result.stdout + result.stderr


@pytest.mark.parametrize('invalid', ['v1', 'missing-policy'])
def test_unsupported_catalog_fails_before_database_connection(database, tmp_path, monkeypatch, invalid):
    from app.macro import enterprise_target_catalog
    from app.cli import macro_enterprise
    from backend.tests.macro.test_enterprise_target_catalog import payload
    value = payload()
    del value['evidencePolicy']
    if invalid == 'v1':
        value['schemaVersion'] = 1
    file = tmp_path / 'unsupported.json'
    file.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setattr(enterprise_target_catalog, 'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE', file)
    def forbidden_connection(*args, **kwargs):
        pytest.fail('invalid configuration must fail before opening database')
    monkeypatch.setattr(macro_enterprise, 'open_readonly_factory', forbidden_connection)
    db_engine, _ = database
    before, config_before = database_snapshot(db_engine), file.read_bytes()
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'ENTERPRISE_TARGET_PREVIEW_FAILED'}
    assert database_snapshot(db_engine) == before and file.read_bytes() == config_before


@pytest.mark.parametrize('option', ['--publish', '--probe', '--force', '--as-of'])
def test_write_or_simulated_date_options_are_not_registered(option):
    result = CliRunner().invoke(app, ['preview-enterprise-targets', option])
    assert result.exit_code == 2
    # 复用项目SafeTyperGroup：解析错误统一脱敏，不回显用户输入。
    assert '--help' in result.stderr and result.stdout == ''


def test_new_help_and_existing_command_help_remain_available():
    result = CliRunner().invoke(app, ['preview-enterprise-targets', '--help'])
    assert result.exit_code == 0
    text = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout)
    assert 'preview-enterprise-targets' in text
    assert all(flag not in text for flag in ['--publish', '--probe', '--force', '--as-of'])
    for command in ['preview-enterprise-rea-inputs', 'audit-macro-enterprise']:
        assert CliRunner().invoke(app, [command, '--help']).exit_code == 0
