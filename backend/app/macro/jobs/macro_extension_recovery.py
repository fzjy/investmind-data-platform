"""显式父ID恢复；不选择新周期，不自动重试或发布。"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.engine import make_url

from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_extension_runtime import LOCK_KEY, execute_extension_group, extension_token
from app.macro.extensions.config import ExtensionError, load_extension_config
from app.macro.extensions.recovery import RecoveryStore
from app.macro.extensions.store import execution_schema
from app.shared.readonly_database import open_readonly_factory


def validate_recovery_ids(parent_ids):
    try:
        if type(parent_ids) is not list or not 1 <= len(parent_ids) <= 2 or len(set(parent_ids)) != len(parent_ids):
            raise ValueError()
        if any(type(value) is not str or str(UUID(value)) != value for value in parent_ids):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ExtensionError('EXTENSION_EXECUTION_INVALID') from None


def _preflight(factory, config, parent_ids, now):
    plans, groups, states = [], set(), []
    for parent_id in parent_ids:
        store = RecoveryStore(factory, parent_id)
        plan = store.recovery_plan(config, now)
        if plan['groupId'] in groups:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        groups.add(plan['groupId'])
        state = store.inspect(plan, now)
        plans.append(plan)
        states.append({'groupId': plan['groupId'], 'parentRunId': parent_id,
            'runId': state['runId'], 'status': state['status'], 'planFingerprint': plan['fingerprint'],
            'scheduledAt': plan['scheduledAt'], 'query': plan['query'], 'reservedCount': state['reservedCount']})
    return plans, states


def run_macro_extension_recovery(settings, parent_ids, *, dry_run=False, transport=None, now=None):
    validate_recovery_ids(parent_ids)  # 拒绝无界/任意ID后才读数据库。
    config = load_extension_config()
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    base = {'readOnly': True, 'supplierHttp': 0, 'publicationRequested': False,
        'recoveryBudgetPerParent': 1, 'maximumRequestedHttp': len(parent_ids),
        'supplierHttpCountBasis': 'transport-attempt-upper-bound'}
    if settings.data_backend != 'database' or not settings.database_url:
        return {**base, 'status': 'blocked', 'errorCode': 'MACRO_EXTENSION_DATABASE_REQUIRED'}
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg':
        return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_POSTGRES_REQUIRED'}
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            problems = execution_schema(factory)
            if problems:
                return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_SCHEMA_REQUIRED', 'problems': problems}
            _, states = _preflight(factory, config, parent_ids, clock())
        return {**base, 'status': 'preview', 'groups': states}
    engine, factory = create_engine_and_session(settings.database_url)
    lock = job_lock_for_engine(engine)
    acquired = False
    try:
        acquired = lock.try_acquire(LOCK_KEY)
        if not acquired:
            return {**base, 'status': 'locked'}
        config = load_extension_config()
        problems = execution_schema(factory)
        if problems:
            return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_SCHEMA_REQUIRED', 'problems': problems}
        plans, _ = _preflight(factory, config, parent_ids, clock())
        from app.connectors.tushare.rate_limit import RequestRateLimiter
        account_rpm = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
        limiter = RequestRateLimiter(min(account_rpm, *(plan['limits']['requestsPerMinute'] for plan in plans)))
        groups = [execute_extension_group(factory, config, plan['groupId'], extension_token(settings, plan['source']), recovery_of=parent,
            transport=transport, rate_limiter=limiter, clock=clock) for plan, parent in zip(plans, parent_ids, strict=True)]
        return {**base, 'readOnly': all(group['readOnly'] for group in groups),
            'supplierHttp': sum(group['supplierHttp'] for group in groups), 'groups': groups,
            'status': 'blocked' if any(group['status'] in ('blocked', 'replay_pending') for group in groups) else 'completed'}
    finally:
        if acquired:
            lock.release(LOCK_KEY)
        engine.dispose()
