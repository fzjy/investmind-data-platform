"""当前v2目录与候选库存只读状态，绝不启用采集/迁移。"""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.jobs.run_errors import JobConfigurationError
from app.macro.enterprise_repository import resolve_enterprise_selection
from app.macro.target_evidence_policy import target_evidence_policy_hash
from app.macro.target_evidence_schema import target_evidence_schema
from app.macro.target_evidence_store import TargetEvidenceStore
from app.macro.target_execution_schema import target_execution_schema
from app.shared.readonly_database import open_readonly_factory


def preview_target_evidence(settings: Settings, *, now: datetime | None = None) -> dict[str, object]:
    from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
    if settings.data_backend != 'database' or not settings.database_url:
        raise JobConfigurationError('ENTERPRISE_DATABASE_REQUIRED')
    catalog = load_enterprise_target_catalog()
    policy = catalog.evidence_policy
    instant = datetime.now(UTC) if now is None else now
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError('TARGET_EVIDENCE_INVALID')
    actual_date = instant.astimezone(ZoneInfo(settings.sync_timezone)).date()
    bootstrap = [target for target in catalog.targets if target.code in policy.bootstrap_target_codes]
    with open_readonly_factory(settings.database_url) as factory:
        selection = resolve_enterprise_selection(factory, settings)
        problems = target_evidence_schema(factory)
        inventory = None if problems else TargetEvidenceStore(factory).inventory()
        execution_problems = target_execution_schema(factory)
    return {'schemaVersion': 1, 'status': 'requirements_pending', 'actualDate': actual_date.isoformat(),
        'catalogHash': catalog.fingerprint, 'policyHash': target_evidence_policy_hash(policy),
        'pool': selection.to_payload(), 'bootstrapTargets': list(policy.bootstrap_target_codes),
        'sourceDependencies': [{'targetCode': t.code, 'kind': t.kind,
            'sourceIndex': None if t.kind == 'etf' else t.code, 'identityVerified': False,
            'requiredApis': ['etf_basic', 'index_basic', 'index_weight'] if t.kind == 'etf' else ['index_basic', 'index_weight']}
            for t in bootstrap], 'schemaReady': not problems, 'problems': list(problems), 'inventory': inventory,
        'collectionImplemented': True, 'physicalBudgetImplemented': True, 'membershipReady': False,
        'collectionEnabled': policy.collection_enabled, 'executionSchemaReady': not execution_problems,
        'executionProblems': list(execution_problems), 'physicalBudgetEnforced': False,
        'supplierHttp': 0, 'readOnly': True}
