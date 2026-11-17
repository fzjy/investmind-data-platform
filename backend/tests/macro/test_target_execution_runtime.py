from backend.tests.support.pg_snapshot import database_snapshot
"""CLI真实只读数据保护及缺schema/关闭开关门。"""
from importlib import import_module
import json

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli.main import app
from backend.tests.macro.test_enterprise_cli import database  # noqa: F401
from backend.tests.macro.test_enterprise_target_catalog import payload
from backend.tests.macro.test_enterprise_target_preview_cli import output


def module():
    try:return import_module('app.macro.jobs.target_execution_runtime')
    except ImportError:pytest.fail('target execution runtime not implemented')


def test_dry_run_missing_execution_schema_is_pending_and_never_migrates(database):
    db_engine,_=database
    from app.db.base import TargetEvidenceBase
    TargetEvidenceBase.metadata.drop_all(db_engine)
    before=database_snapshot(db_engine)
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence','--dry-run'])
    assert result.exit_code==0,result.stdout
    value=output(result)
    assert value['schemaReady'] is False and value['readOnly'] and value['supplierHttp']==0
    assert value['executorImplemented'] and value['physicalBudgetImplemented']
    assert value['membershipReady'] is False and not value['physicalBudgetEnforced']
    assert database_snapshot(db_engine)==before


def test_explicit_disabled_entry_has_no_plan_or_transport_side_effect(database,tmp_path,monkeypatch):
    from app.macro import enterprise_target_catalog
    file=tmp_path/'disabled.json'
    file.write_text(json.dumps(payload()),encoding='utf8')
    monkeypatch.setattr(enterprise_target_catalog,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',file)
    db_engine,_=database;before=database_snapshot(db_engine)
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence'])
    assert result.exit_code==0,result.stdout
    value=output(result)
    assert value['status']=='disabled' and value['readOnly'] and value['requestCount']==0
    assert database_snapshot(db_engine)==before


def test_formal_tables_preview_still_does_not_freeze_a_root(database):
    db_engine,factory=database;module()
    from app.db.models.enterprise_target_execution import TargetExecutionPlan
    before=database_snapshot(db_engine)
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence','--dry-run'])
    assert result.exit_code==0,result.stdout
    value=output(result)
    assert value['schemaReady'] and value['rootReservedCount']==0
    assert not value['processingCompleted'] and value['requestCount']==0
    assert database_snapshot(db_engine)==before
    with factory() as s:assert s.scalars(select(TargetExecutionPlan)).all()==[]


def test_invalid_catalog_is_sanitized_before_database_capture(database,tmp_path,monkeypatch):
    runtime=module();from app.macro import enterprise_target_catalog
    value=payload();value['evidencePolicy']['maxHttpPerRun']='sensitive-marker'
    file=tmp_path/'bad.json';file.write_text(json.dumps(value),encoding='utf8')
    monkeypatch.setattr(enterprise_target_catalog,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',file)
    def forbidden(*args,**kwargs):pytest.fail('invalid catalog must not open database')
    monkeypatch.setattr(runtime,'open_readonly_factory',forbidden)
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence','--dry-run'])
    assert result.exit_code==2
    assert 'sensitive-marker' not in result.stdout+result.stderr
    assert output(result)['errorCode']=='TARGET_EXECUTION_FAILED'


def test_enabled_policy_cannot_write_foreign_dialect(database,tmp_path,monkeypatch):
    monkeypatch.setenv("DANG_DATABASE_URL", "mysql://invalid/rejected")
    module();from app.macro import enterprise_target_catalog
    db_engine,_=database;value=payload();value['evidencePolicy']['collectionEnabled']=True
    file=tmp_path/'enabled.json';file.write_text(json.dumps(value),encoding='utf8')
    monkeypatch.setattr(enterprise_target_catalog,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',file)
    before=database_snapshot(db_engine);result=CliRunner().invoke(app,['sync-enterprise-target-evidence'])
    assert result.exit_code==2 and output(result)['errorCode']=='TARGET_POSTGRES_REQUIRED'
    assert database_snapshot(db_engine)==before


@pytest.mark.parametrize('option',['--force','--retry','--as-of','--target','--source','--publish'])
def test_no_escape_hatches_added_to_manual_entry(database,option):
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence',option])
    assert result.exit_code==2
