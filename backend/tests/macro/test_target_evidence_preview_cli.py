from backend.tests.support.configuration import assert_fixture_configuration_rejected
"""新预览只读消费v2及新表，不因缺表自动迁移或采集。"""
from datetime import datetime
from importlib import import_module
import json
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import MetaData
from sqlalchemy.sql.schema import BLANK_SCHEMA
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from backend.tests.macro.test_enterprise_cli import database
from backend.tests.macro.test_enterprise_target_catalog import payload
from backend.tests.macro.test_enterprise_target_preview_cli import output
from backend.tests.macro.test_target_evidence_normalize import request, response, CAPTURED, AS_OF
from backend.tests.macro.test_target_evidence_policy import policy


def test_preview_missing_schema_reports_pending_and_preserves_bytes(database):
    path, _ = database
    before = path.read_bytes()
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == 0, result.stdout
    value = output(result)
    assert value['schemaVersion'] == 1 and value['schemaReady'] is False
    assert len(value['problems']) == 2 and value['inventory'] is None
    assert value['pool']['size'] == 50 and len(value['bootstrapTargets']) == 2
    assert value['actualDate'] == datetime.now(ZoneInfo(Settings.from_env().sync_timezone)).date().isoformat()
    assert len(value['catalogHash']) == 64 and len(value['policyHash']) == 64
    assert value['collectionImplemented'] and value['physicalBudgetImplemented']
    assert value['membershipReady'] is False and value['executionSchemaReady'] is False
    assert value['supplierHttp'] == 0 and value['readOnly'] is True and 'policyConfigured' not in value
    assert value['sourceDependencies'][1]['sourceIndex'] is None
    assert path.read_bytes() == before


def test_preview_existing_candidates_remain_not_proven_and_readonly(database):
    path, factory = database
    from app.db.base import TargetEvidenceBase
    from app.macro.target_evidence_store import TargetEvidenceStore
    metadata = MetaData()
    for table in TargetEvidenceBase.metadata.sorted_tables:
        table.to_metadata(metadata, schema=None, referred_schema_fn=lambda *args: BLANK_SCHEMA)
    metadata.create_all(factory.kw['bind'])
    store = TargetEvidenceStore(factory)
    key = store.receive(request(), response(), CAPTURED, policy())
    store.normalize(key, policy(), AS_OF, 'Asia/Shanghai', persist=True)
    before = path.read_bytes()
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == 0, result.stdout
    value = output(result)
    assert value['schemaReady'] is True and value['inventory'] == {'observationCount': 1, 'candidateCount': 1}
    assert value['membershipReady'] is False and path.read_bytes() == before
    assert '"members":' not in result.stdout and '"capturedAt":' not in result.stdout


@pytest.mark.parametrize('count', [0, 61])
def test_preview_custom_pool_not_fixed_or_whole_market(database, tmp_path, monkeypatch, count):
    path, _ = database
    file = tmp_path / 'pool.json'
    file.write_text(json.dumps([f'{i:06}.SZ' for i in range(1, count + 1)]), encoding='utf-8')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE', 'custom')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE_FILE', str(file))
    before = path.read_bytes()
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == (0 if count else 2)
    assert (output(result)['pool']['size'] == count if count else output(result)['errorCode'] == 'TARGET_EVIDENCE_PREVIEW_FAILED')
    assert path.read_bytes() == before


@pytest.mark.parametrize('invalid', ['v1', 'missing-policy', 'bad-policy'])
def test_preview_bad_catalog_fails_before_opening_database(database, tmp_path, monkeypatch, invalid):
    from app.macro import enterprise_target_catalog
    try:
        preview = import_module('app.macro.target_evidence_preview')
    except ImportError:
        pytest.fail('target evidence preview missing')
    value = payload()
    if invalid in ('v1', 'missing-policy'): del value['evidencePolicy']
    if invalid == 'v1': value['schemaVersion'] = 1
    if invalid == 'bad-policy': value['evidencePolicy']['maxHttpPerRun'] = 'private-value'
    file = tmp_path / 'invalid.json'
    file.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setattr(enterprise_target_catalog, 'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE', file)
    def forbidden(*args, **kwargs): pytest.fail('bad config must fail before database connection')
    monkeypatch.setattr(preview, 'open_readonly_factory', forbidden)
    before = file.read_bytes()
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'TARGET_EVIDENCE_PREVIEW_FAILED'}
    assert 'private-value' not in result.stdout + result.stderr and file.read_bytes() == before


def test_enabled_policy_does_not_add_collection_capability(database, tmp_path, monkeypatch):
    from app.macro import enterprise_target_catalog
    value = payload(); value['evidencePolicy']['collectionEnabled'] = True
    file = tmp_path / 'enabled.json'; file.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setattr(enterprise_target_catalog, 'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE', file)
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == 0, result.stdout
    assert output(result)['supplierHttp'] == 0 and output(result)['collectionImplemented'] is True
    assert output(result)['executionSchemaReady'] is False and output(result)['membershipReady'] is False


@pytest.mark.parametrize('mode', ['fixture', 'missing-url'])
def test_preview_requires_database_with_safe_classification(monkeypatch, mode):
    if mode == 'fixture':
        assert_fixture_configuration_rejected(monkeypatch)
        return
    monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
    if mode == 'missing-url':
        monkeypatch.delenv('DANG_DATABASE_URL', raising=False)
        monkeypatch.delenv('DANG_DATABASE_READ_URL', raising=False)
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == 2
    assert output(result) == {'status': 'failed', 'errorCode': 'ENTERPRISE_DATABASE_REQUIRED'}


@pytest.mark.parametrize('failure', ['file', 'driver', 'timezone', 'validation'])
def test_preview_expected_failures_do_not_leak_details(database, monkeypatch, failure):
    from app.macro import enterprise_target_catalog
    private = 'private-host-password-do-not-echo'
    if failure == 'file':
        def fail(*args, **kwargs): raise OSError(private)
        monkeypatch.setattr(enterprise_target_catalog, 'load_enterprise_target_catalog', fail)
    else:
        monkeypatch.setenv({'driver': 'DANG_DATABASE_URL', 'timezone': 'DANG_SYNC_TIMEZONE',
            'validation': 'DANG_MACRO_ENTERPRISE_MIN_SAMPLES'}[failure], f'invalid://{private}' if failure == 'driver' else private)
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence'])
    assert result.exit_code == 2
    assert output(result)['errorCode'] == 'TARGET_EVIDENCE_PREVIEW_FAILED'
    assert private not in result.stdout + result.stderr


@pytest.mark.parametrize('option', ['--publish', '--probe', '--force', '--as-of'])
def test_preview_no_write_or_date_override_options(option):
    result = CliRunner().invoke(app, ['preview-enterprise-target-evidence', option])
    assert result.exit_code == 2 and result.stdout == '' and '--help' in result.stderr


def test_preview_help_and_old_command_output_remain_available(database):
    assert CliRunner().invoke(app, ['preview-enterprise-target-evidence', '--help']).exit_code == 0
    result = CliRunner().invoke(app, ['preview-enterprise-targets'])
    assert result.exit_code == 0 and output(result)['schemaVersion'] == 1
