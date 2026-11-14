"""CLI真实只读字节保护及缺schema/关闭开关门。"""
from importlib import import_module
import json

import pytest
from sqlalchemy import MetaData,select
from sqlalchemy.sql.schema import BLANK_SCHEMA
from typer.testing import CliRunner

from app.cli.main import app
from backend.tests.macro.test_enterprise_cli import database  # noqa: F401
from backend.tests.macro.test_enterprise_target_catalog import payload
from backend.tests.macro.test_enterprise_target_preview_cli import output


def module():
    try:return import_module('app.macro.jobs.target_execution_runtime')
    except ImportError:pytest.fail('target execution runtime not implemented')


def test_dry_run_missing_execution_schema_is_pending_and_never_migrates(database):
    path,_=database;before=path.read_bytes()
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence','--dry-run'])
    assert result.exit_code==0,result.stdout
    value=output(result)
    assert value['schemaReady'] is False and value['readOnly'] and value['supplierHttp']==0
    assert value['executorImplemented'] and value['physicalBudgetImplemented']
    assert value['membershipReady'] is False and not value['physicalBudgetEnforced']
    assert path.read_bytes()==before


def test_explicit_disabled_entry_has_no_plan_or_transport_side_effect(database,tmp_path,monkeypatch):
    from app.macro import enterprise_target_catalog
    file=tmp_path/'disabled.json'
    file.write_text(json.dumps(payload()),encoding='utf8')
    monkeypatch.setattr(enterprise_target_catalog,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',file)
    path,_=database;before=path.read_bytes()
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence'])
    assert result.exit_code==0,result.stdout
    value=output(result)
    assert value['status']=='disabled' and value['readOnly'] and value['requestCount']==0
    assert path.read_bytes()==before


def test_formal_tables_preview_still_does_not_freeze_a_root(database):
    path,factory=database;module()
    from app.db.base import TargetEvidenceBase
    from app.db.models.enterprise_target_execution import TargetExecutionPlan
    metadata=MetaData()
    for table in TargetEvidenceBase.metadata.sorted_tables:
        table.to_metadata(metadata,schema=None,referred_schema_fn=lambda *args:BLANK_SCHEMA)
    metadata.create_all(factory.kw['bind']);before=path.read_bytes()
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence','--dry-run'])
    assert result.exit_code==0,result.stdout
    value=output(result)
    assert value['schemaReady'] and value['rootReservedCount']==0
    assert not value['processingCompleted'] and value['requestCount']==0
    assert path.read_bytes()==before
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


def test_enabled_policy_cannot_write_sqlite(database,tmp_path,monkeypatch):
    module();from app.macro import enterprise_target_catalog
    path,_=database;value=payload();value['evidencePolicy']['collectionEnabled']=True
    file=tmp_path/'enabled.json';file.write_text(json.dumps(value),encoding='utf8')
    monkeypatch.setattr(enterprise_target_catalog,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',file)
    before=path.read_bytes();result=CliRunner().invoke(app,['sync-enterprise-target-evidence'])
    assert result.exit_code==2 and output(result)['errorCode']=='TARGET_POSTGRES_REQUIRED'
    assert path.read_bytes()==before


@pytest.mark.parametrize('option',['--force','--retry','--as-of','--target','--source','--publish'])
def test_no_escape_hatches_added_to_manual_entry(database,option):
    result=CliRunner().invoke(app,['sync-enterprise-target-evidence',option])
    assert result.exit_code==2
