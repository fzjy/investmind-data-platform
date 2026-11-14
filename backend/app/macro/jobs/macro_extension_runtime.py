"""独立扩展组正常入口；不发布、不启动调度器、不重发未知或失败请求。"""
from datetime import datetime, timezone

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError, load_extension_config, selected_extension_groups
from app.macro.extensions.plan import build_extension_plan
from app.macro.extensions.store import ExtensionStore, FAILURE_CODES, execution_schema, instant, run_key


LOCK_KEY = 'macro-extension-execution-v1'


def execute_extension_group(factory, config, group_id, token, *, transport=None, now=None, rate_limiter=None, clock=None, before_send=None, recovery_of=None):
    clock = clock or (lambda: instant(now) if now is not None else datetime.now(timezone.utc))
    checked_at = clock()
    if recovery_of is None:
        plan = build_extension_plan(group_id, config, checked_at)
        store = ExtensionStore(factory)
    else:
        from app.macro.extensions.recovery import RecoveryStore
        store = RecoveryStore(factory, recovery_of)
        plan = store.recovery_plan(config, checked_at)
        if plan['groupId'] != group_id:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
    result = {'groupId': group_id, 'planFingerprint': plan['fingerprint'], 'supplierHttp': 0,
        'requestCount': 0, 'responseCount': 0, 'insertedRows': 0, 'readOnly': True,
        'physicalBudgetEnforced': True, 'publicationRequested': False,
        'supplierHttpCountBasis': 'transport-attempt-upper-bound',
        'requestCountBasis': 'committed-reservation-upper-bound'}
    if recovery_of is not None:
        result['parentRunId'] = recovery_of
    state = store.inspect(plan, checked_at)
    result['planPersisted'] = state['runId'] is not None
    if not config.groups[group_id].collectionEnabled:
        return {**result, 'status': 'disabled'}
    if state['status'] == 'completed':
        return {**result, 'status': 'completed', 'runId': state['runId']}
    if state['status'] in ('failed', 'uncertain'):
        return {**result, 'status': 'blocked', 'runId': state['runId'],
            'errorCode': state['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN'}
    key = state['runId']
    if state['status'] == 'new':
        if not token:
            return {**result, 'status': 'blocked', 'errorCode': 'EXTENSION_AUTH_ERROR'}
        reserved = False
        reservation_attempted = False
        def reserve():
            nonlocal key, reserved, reservation_attempted
            if rate_limiter is not None:
                rate_limiter.acquire()
            if before_send is not None:
                before_send()
            current = load_extension_config()
            plan_time = datetime.fromisoformat(plan['scheduledAt']) if recovery_of is not None else clock()
            current_plan = build_extension_plan(group_id, current, plan_time)
            if not current.groups[group_id].collectionEnabled or current_plan['fingerprint'] != plan['fingerprint']:
                raise ExtensionError('EXTENSION_COLLECTION_CHANGED')
            result['readOnly'] = False  # 已尝试写事务；提交结果不明时不能声称纯只读。
            key = store.attempt_key(plan)
            reservation_attempted = True
            store.reserve(plan, clock())
            reserved = True
            result.update(requestCount=1, supplierHttp=1, readOnly=False, planPersisted=True)
        try:
            from app.macro.extensions.fred_client import FredExtensionClient
            client_type = FredExtensionClient if plan['source'] == 'fred' else ExtensionClient
            with client_type(token, transport=transport) as client:
                data = client.call(plan, reserve)
            result['responseCount'] = 1
        except Exception as error:
            code = str(error) if isinstance(error, ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
            if reserved:
                try:
                    store.fail(key, code, clock())
                except (ValueError, SQLAlchemyError, OSError, RuntimeError):
                    code = 'EXTENSION_ATTEMPT_UNCERTAIN'
            elif reservation_attempted:
                # COMMIT确认丢失不代表没有预留。回读主源；回读也失败则保守保留上界。
                try:
                    confirmed = store.inspect(plan, clock())
                    persisted = confirmed['runId'] == key
                    result.update(requestCount=int(persisted), planPersisted=persisted)
                except (ValueError, SQLAlchemyError, OSError, RuntimeError):
                    result.update(requestCount=1, planPersisted=None, reservationUncertain=True)
                code = 'EXTENSION_RESERVATION_UNCERTAIN'
            return {**result, 'status': 'blocked', 'errorCode': code, 'runId': key}
        # Raw失败保留reserved/未知，不假报退款或重试；外层只输出安全错误码。
        try:
            store.receive(key, data, clock())
        except (ValueError, SQLAlchemyError, OSError, RuntimeError):
            return {**result, 'status': 'blocked', 'errorCode': 'EXTENSION_RAW_RECEIVE_PENDING', 'runId': key}
    try:
        replay = store.replay(key, clock=clock)
    except (ValueError, SQLAlchemyError, OSError, RuntimeError):
        return {**result, 'status': 'replay_pending', 'errorCode': 'EXTENSION_CORE_REPLAY_PENDING', 'runId': key}
    return {**result, **replay, 'readOnly': False, 'status': 'completed', 'runId': key,
        'planPersisted': True}


def extension_token(settings, source):
    if source not in ('fred', 'tushare'):
        raise ExtensionError('EXTENSION_EXECUTION_INVALID')
    secret = settings.fred_api_key if source == 'fred' else settings.tushare_token
    return secret.get_secret_value() if secret else ''


def run_macro_extensions(settings, *, dry_run=False, transport=None, now=None, before_send=None, group_ids=None):
    from app.macro.extensions.preview import preview_macro_extensions
    config = load_extension_config()  # 配置坏时不能打开数据库或网络。
    selected = selected_extension_groups(config, group_ids)
    base = {'readOnly': True, 'executorImplemented': True, 'physicalBudgetImplemented': True,
        'physicalBudgetEnforced': False, 'supplierHttp': 0, 'publicationRequested': False}
    if settings.data_backend != 'database' or not settings.database_url:
        return {**base, 'status': 'blocked', 'errorCode': 'MACRO_EXTENSION_DATABASE_REQUIRED'}
    if dry_run:
        return {**preview_macro_extensions(settings, now=now, group_ids=selected), 'status': 'preview'}
    if not any(config.groups[key].collectionEnabled for key in selected):
        return {**base, 'status': 'disabled'}
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg':
        return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_POSTGRES_REQUIRED'}
    engine, factory = create_engine_and_session(settings.database_url)
    lock = job_lock_for_engine(engine)
    acquired = False
    try:
        acquired = lock.try_acquire(LOCK_KEY)
        if not acquired:
            return {**base, 'status': 'locked'}
        config = load_extension_config()
        selected = selected_extension_groups(config, group_ids)
        problems = execution_schema(factory)
        if problems:
            return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_SCHEMA_REQUIRED', 'problems': problems}
        groups = []
        from app.connectors.tushare.rate_limit import RequestRateLimiter
        account_rpm = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
        rate = min(account_rpm, *(group.limits.requestsPerMinute for group in config.groups.values()))
        limiter = RequestRateLimiter(rate)
        for group_id in selected:
            try:
                from app.macro.exchange_summary_config import EXCHANGE_SUMMARY_GROUPS
                if group_id in EXCHANGE_SUMMARY_GROUPS:
                    from app.macro.jobs.exchange_summary_runtime import execute_exchange_group
                    groups.append(execute_exchange_group(factory,config,group_id,extension_token(settings,'tushare'),
                        transport=transport,clock=(lambda:instant(now)) if now is not None else None,
                        rate_limiter=limiter,before_send=before_send))
                    continue
                from app.macro.extensions.config import OFFICIAL_MACRO_GROUPS
                if group_id in OFFICIAL_MACRO_GROUPS:
                    from app.macro.jobs.official_macro_runtime import execute_official_macro_group
                    groups.append(execute_official_macro_group(factory, config, group_id, transport=transport,
                        clock=(lambda: instant(now)) if now is not None else None, rate_limiter=limiter, before_send=before_send))
                    continue
                if group_id == 'governmentBonds':
                    from app.macro.jobs.macro_fiscal_runtime import execute_fiscal_group
                    groups.append(execute_fiscal_group(factory, config, transport=transport,
                        clock=(lambda: instant(now)) if now is not None else None, rate_limiter=limiter, before_send=before_send))
                    continue
                from app.macro.extensions.plan import CONTRACTS
                token = extension_token(settings, CONTRACTS[group_id]['source'])
                groups.append(execute_extension_group(factory, config, group_id, token, transport=transport, now=now, rate_limiter=limiter, before_send=before_send))
            except (ValueError, SQLAlchemyError, OSError, RuntimeError, KeyError):
                groups.append({'groupId': group_id, 'status': 'blocked', 'errorCode': 'EXTENSION_EXECUTION_INVALID',
                    'supplierHttp': 0, 'readOnly': True})
        return {**base, 'groups': groups, 'physicalBudgetEnforced': True,
            'readOnly': all(group['readOnly'] for group in groups),
            'supplierHttp': sum(group['supplierHttp'] for group in groups),
            'status': ('blocked' if any(group['status'] in ('blocked', 'replay_pending') for group in groups) else
                'partial' if any(group['status'] in ('partial','source_pending') for group in groups) else 'completed'),
            'supplierHttpCountBasis': 'transport-attempt-upper-bound'}
    finally:
        if acquired:
            lock.release(LOCK_KEY)
        engine.dispose()
