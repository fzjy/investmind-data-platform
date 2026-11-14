"""独立有界执行入口；dry-run只读，正式写入需PG/schema/独立锁。"""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.target_execution_batch import execute_target_batch
from app.macro.enterprise_repository import resolve_enterprise_selection
from app.macro.target_evidence_policy import target_evidence_policy_hash
from app.macro.target_execution_schema import target_execution_schema
from app.macro.target_execution_store import TargetExecutionStore
from app.shared.readonly_database import open_readonly_factory

LOCK_KEY = 'enterprise-target-execution'


def _capture(settings, catalog, instant):
    as_of = instant.astimezone(ZoneInfo(settings.sync_timezone)).date()
    with open_readonly_factory(settings.database_url) as factory:
        selection = resolve_enterprise_selection(factory, settings)
        problems = target_execution_schema(factory)
        summary = {'schemaReady': not problems, 'executionSchemaReady': not problems,
            'problems': list(problems), 'pool': selection.to_payload(),
            'catalogHash': catalog.fingerprint,
            'policyHash': target_evidence_policy_hash(catalog.evidence_policy),
            'collectionEnabled': catalog.evidence_policy.collection_enabled,
            'rootReservedCount': 0, 'replayPendingCount': 0,
            'processingCompleted': False, 'blockedReasons': [], 'actualDate': as_of.isoformat()}
        if not problems:
            store = TargetExecutionStore(factory)
            root = store.prepare(catalog, selection, as_of, settings.sync_timezone)
            summary.update(store.progress(root), planFingerprint=root['fingerprint'],
                frozenAsOfDate=root['snapshot']['frozenAsOfDate'])
    return selection, as_of, summary


def run_target_execution(settings, *, dry_run=False, transport=None, now=None):
    from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
    base = {'executorImplemented': True, 'physicalBudgetImplemented': True,
        'physicalBudgetEnforced': False, 'readOnly': True, 'requestCount': 0,
        'responseCount': 0, 'supplierHttp': 0, 'membershipReady': False,
        'publicationRequested': False,
        'supplierHttpCountBasis': 'committed-reservation-attempt-upper-bound'}
    def blocked(code):
        return {**base, 'status': 'blocked', 'errorCode': code}
    if settings.data_backend != 'database' or not settings.database_url:
        return blocked('TARGET_DATABASE_REQUIRED')
    catalog = load_enterprise_target_catalog()  # 坏配置必须在打开数据库之前失败。
    instant = datetime.now(UTC) if now is None else now
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError('TARGET_EXECUTION_INVALID')
    if not dry_run and not catalog.evidence_policy.collection_enabled:
        return {**base, 'status': 'disabled', 'collectionEnabled': False}
    url = make_url(settings.database_url)
    if not dry_run and (url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg'):
        return blocked('TARGET_POSTGRES_REQUIRED')
    _, _, summary = _capture(settings, catalog, instant)
    if summary['blockedReasons']:
        return {**base, **summary, 'status': 'blocked', 'errorCode': summary['blockedReasons'][0]}
    if dry_run:
        return {**base, **summary, 'status': 'preview' if summary['schemaReady'] else 'pending'}
    if not summary['schemaReady']:
        return {**blocked('TARGET_EXECUTION_SCHEMA_REQUIRED'), **summary}
    engine, factory = create_engine_and_session(settings.database_url)
    lock = job_lock_for_engine(engine)
    acquired = False
    try:
        acquired = lock.try_acquire(LOCK_KEY)
        if not acquired:
            return {**base, 'status': 'locked'}
        # 不沿用锁前目录、股票池或库存；关闭开关/漂移均不得继续发请求。
        catalog = load_enterprise_target_catalog()
        if not catalog.evidence_policy.collection_enabled:
            return {**base, 'status': 'disabled', 'collectionEnabled': False}
        selection, as_of, summary = _capture(settings, catalog, instant)
        if summary['blockedReasons']:
            return {**base, **summary, 'status': 'blocked', 'errorCode': summary['blockedReasons'][0]}
        if not summary['schemaReady']:
            return {**blocked('TARGET_EXECUTION_SCHEMA_REQUIRED'), **summary}
        token = settings.tushare_token.get_secret_value() if settings.tushare_token else ''
        result = execute_target_batch(factory, catalog, selection, as_of,
            settings.sync_timezone, token, transport=transport, now=now)
        return {**base, **summary, **result, 'physicalBudgetEnforced': True}
    finally:
        if acquired:
            lock.release(LOCK_KEY)
        engine.dispose()
