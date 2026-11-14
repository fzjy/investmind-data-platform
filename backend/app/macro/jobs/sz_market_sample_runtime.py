"""手动一次性原件采样；固定总额度，不进入日常调度或Core。"""
import asyncio
from datetime import datetime, timezone

from app.macro.extensions.client import ExtensionClient, decode_json
from app.macro.extensions.config import ExtensionError, load_extension_config
from app.macro.extensions.plan import canonical
from app.macro.extensions.store import FAILURE_CODES
from app.macro.sz_market_sample import SzMarketSamplePolicy, build_sample_plan, safe_sample
from app.macro.sz_market_sample_store import SzMarketSampleStore


async def fetch_sample(plan, token, transport):
    body = await ExtensionClient(token, transport=transport)._read(plan, 'POST', plan['policy']['providerUrl'],
        payload={'api_name': 'sz_daily_info', 'token': token, 'params': plan['query']['params'],
            'fields': ','.join(plan['query']['fields'])})
    try:
        value = decode_json(body)
        if type(value) is not dict or type(value.get('code')) is not int:
            raise ValueError()
        if value['code'] != 0:
            raise ExtensionError('EXTENSION_PERMISSION_ERROR' if value['code'] == 2002 else 'EXTENSION_PROVIDER_REJECTED')
        if token in canonical(value):
            raise ValueError()
        data = value['data']
        if type(data) is not dict or 'has_more' in data and data['has_more'] is not False:
            raise ValueError()
        return safe_sample({'fields': data['fields'], 'items': data['items']}, plan)
    except ExtensionError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None


def execute_sz_market_sample(factory, policy, token, *, transport=None, clock=None, policy_loader=None, dry_run=False):
    clock = clock or (lambda: datetime.now(timezone.utc))
    base = {'readOnly': True, 'supplierHttp': 0, 'publicationRequested': False, 'coreEligible': False, 'maxTotalHttp': 1}
    base['supplierHttpCountBasis'] = 'transport-attempt-upper-bound'
    if not policy.enabled:
        return {**base, 'status': 'disabled'}
    plan = build_sample_plan(factory, policy, clock())
    store = SzMarketSampleStore(factory)
    state = store.inspect(plan, clock())
    if dry_run:
        return {**base, 'status': 'preview', 'plan': plan, 'pilot': state}
    if state['status'] != 'new':
        return {**base, **state, 'status': 'captured' if state['status'] == 'captured' else 'blocked'}
    if not token:
        return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_AUTH_ERROR'}
    policy_loader = policy_loader or (lambda: load_extension_config().contractSamples['szMarket'])
    latest = SzMarketSamplePolicy.model_validate(policy_loader())
    if latest != policy:
        return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_COLLECTION_CHANGED'}
    # 确认预留提交后才构造网络会话；COMMIT不确定直接向外失败，不发送。
    try:
        store.reserve(plan, clock())
    except Exception:
        return {**base, 'readOnly': False, 'status': 'blocked', 'errorCode': 'EXTENSION_RESERVATION_UNCERTAIN'}
    base = {**base, 'readOnly': False, 'supplierHttp': 0, 'requestCount': 1}
    attempted = False
    try:
        latest = SzMarketSamplePolicy.model_validate(policy_loader())
        if latest != policy:
            store.finish(plan, clock(), error='EXTENSION_COLLECTION_CHANGED')
            return {**base, 'supplierHttp': 0, 'status': 'blocked', 'errorCode': 'EXTENSION_COLLECTION_CHANGED'}
        attempted = True
        data = asyncio.run(fetch_sample(plan, token, transport))
    except Exception as error:
        code = str(error) if isinstance(error, ExtensionError) and str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
        try:
            store.finish(plan, clock(), error=code)
        except Exception:
            code = 'EXTENSION_ATTEMPT_UNCERTAIN'
        return {**base, 'supplierHttp': int(attempted), 'status': 'blocked', 'errorCode': code}
    try:
        store.finish(plan, clock(), data=data)
        return {**base, 'supplierHttp': 1, **store.inspect(plan, clock())}
    except Exception:
        return {**base, 'supplierHttp': 1, 'status': 'blocked', 'errorCode': 'EXTENSION_RAW_RECEIVE_PENDING'}


def run_sz_market_sample(settings, *, dry_run=False):
    from sqlalchemy.engine import make_url
    from app.db.session import create_engine_and_session
    from app.macro.jobs.macro_extension_runtime import extension_token
    from app.macro.extensions.store import execution_schema
    from app.shared.readonly_database import open_readonly_factory
    base = {'status': 'disabled', 'readOnly': True, 'supplierHttp': 0, 'publicationRequested': False}
    policy = load_extension_config().contractSamples.get('szMarket')
    if policy is None or not policy.enabled:
        return base
    if settings.data_backend != 'database' or not settings.database_url:
        return {**base, 'status': 'blocked', 'errorCode': 'MACRO_EXTENSION_DATABASE_REQUIRED'}
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            return execute_sz_market_sample(factory, policy, '', dry_run=True)
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg':
        return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_POSTGRES_REQUIRED'}
    engine, factory = create_engine_and_session(settings.database_url)
    try:
        if execution_schema(factory):
            return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_SCHEMA_REQUIRED'}
        return execute_sz_market_sample(factory, policy, extension_token(settings, 'tushare'))
    finally:
        engine.dispose()
