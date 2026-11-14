"""官方政府债组；统一扩展入口下的独立预算，无密钥和自动重试。"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError, load_extension_config
from app.macro.extensions.plan import build_extension_plan, validate_extension_plan
from app.macro.extensions.store import instant
from app.macro.fiscal_store import FiscalStore, ERRORS, run_id


class OfficialFiscalClient:
    def __init__(self, transport=None):
        # 只复用有界字节读取；此内部标记不进入请求，不读取任何供应商secret。
        self.reader = ExtensionClient('public-document', transport=transport)

    def fetch(self, plan, document):
        validate_extension_plan(plan)
        if plan['groupId'] != 'governmentBonds' or document not in plan['documents']:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        return bytes(asyncio.run(self.reader._read(plan, 'GET', document['url'])))


def execute_fiscal_group(factory, config, *, transport=None, clock=None, rate_limiter=None, before_send=None):
    result = {'groupId': 'governmentBonds', 'planFingerprint': None, 'supplierHttp': 0,
        'requestCount': 0, 'responseCount': 0, 'insertedRows': 0, 'readOnly': True,
        'physicalBudgetEnforced': True, 'publicationRequested': False,
        'supplierHttpCountBasis': 'transport-attempt-upper-bound', 'documents': []}
    try:
        return _execute_fiscal_group(factory, config, result, transport=transport, clock=clock,
            rate_limiter=rate_limiter, before_send=before_send)
    except Exception:
        return {**result, 'status': 'blocked', 'errorCode': 'EXTENSION_ATTEMPT_UNCERTAIN' if not result['readOnly'] else 'EXTENSION_EXECUTION_INVALID'}


def _execute_fiscal_group(factory, config, result, *, transport=None, clock=None, rate_limiter=None, before_send=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    plan = build_extension_plan('governmentBonds', config, clock())
    store = FiscalStore(factory)
    result['planFingerprint'] = plan['fingerprint']
    if not config.groups['governmentBonds'].collectionEnabled:
        return {**result, 'status': 'disabled'}
    state = store.inspect(plan, clock())
    plan = state['plan']
    result['planFingerprint'] = plan['fingerprint']
    if state['status'] == 'blocked':
        blocked = next(d for d in state['documents'] if d['status'] in ('reserved', 'failed'))
        return {**result, 'status': 'blocked', 'errorCode': blocked['errorCode'] or 'EXTENSION_ATTEMPT_UNCERTAIN'}
    for entry in state['documents']:
        doc, key = entry['document'], entry['runId']
        if entry['status'] == 'completed':
            continue
        if entry['status'] == 'new':
            if result['requestCount'] >= plan['limits']['maxHttpPerRun']:
                break
            if rate_limiter is not None:
                rate_limiter.acquire()
            if before_send is not None:
                before_send()
            current = load_extension_config()
            checked = build_extension_plan('governmentBonds', current, datetime.fromisoformat(plan['scheduledAt']))
            if not current.groups['governmentBonds'].collectionEnabled or checked != plan:
                return {**result, 'status': 'blocked', 'errorCode': 'EXTENSION_COLLECTION_CHANGED'}
            key = run_id(plan, doc)
            result['readOnly'] = False
            try:
                store.reserve(plan, doc, clock())
            except (ValueError, SQLAlchemyError, OSError, RuntimeError):
                return {**result, 'status': 'blocked', 'errorCode': 'EXTENSION_RESERVATION_UNCERTAIN',
                    'reservationUncertain': True, 'requestCount': result['requestCount'] + 1}
            result['requestCount'] += 1
            result['supplierHttp'] += 1
            try:
                body = OfficialFiscalClient(transport).fetch(plan, doc)
                result['responseCount'] += 1
            except Exception as error:
                code = str(error) if isinstance(error, ExtensionError) and str(error) in ERRORS else 'EXTENSION_EXECUTION_FAILED'
                try:
                    store.fail(key, code, clock())
                except (ValueError, SQLAlchemyError, OSError, RuntimeError):
                    code = 'EXTENSION_ATTEMPT_UNCERTAIN'
                return {**result, 'status': 'blocked', 'errorCode': code}
            try:
                store.receive(key, body, clock())
            except (ValueError, SQLAlchemyError, OSError, RuntimeError):
                return {**result, 'status': 'replay_pending', 'errorCode': 'EXTENSION_RAW_RECEIVE_PENDING'}
        result['readOnly'] = False
        try:
            replay = store.replay(key, clock=clock)
        except ExtensionError as error:
            if str(error) == 'FISCAL_TABLE_INVALID':
                store.fail(key, 'FISCAL_TABLE_INVALID', clock())
                return {**result, 'status': 'blocked', 'errorCode': 'FISCAL_TABLE_INVALID'}
            return {**result, 'status': 'replay_pending', 'errorCode': 'EXTENSION_CORE_REPLAY_PENDING'}
        except (ValueError, SQLAlchemyError, OSError, RuntimeError):
            return {**result, 'status': 'replay_pending', 'errorCode': 'EXTENSION_CORE_REPLAY_PENDING'}
        result['insertedRows'] += replay['insertedRows']
        result['documents'].append({'year': doc['year'], 'runId': key, 'status': 'completed', **replay})
    final = store.inspect(plan, instant(clock()))
    return {**result, 'status': final['status'], 'planPersisted': final['runId'] is not None}


def run_fiscal_raw_replay(settings, failed_run_id, *, dry_run=False):
    """只重放明确原ID的已存原件；没有供应商传输路径。"""
    from uuid import UUID
    from sqlalchemy.engine import make_url
    from app.db.models.ops import JobRun
    from app.db.session import create_engine_and_session
    from app.jobs.locks import job_lock_for_engine
    from app.macro.jobs.macro_extension_runtime import LOCK_KEY
    from app.shared.readonly_database import open_readonly_factory
    base = {'readOnly':True,'supplierHttp':0,'publicationRequested':False}
    try:
        if type(failed_run_id) is not str or str(UUID(failed_run_id)) != failed_run_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        return {**base,'status':'blocked','errorCode':'FISCAL_RAW_REPLAY_ID_INVALID'}
    base['sourceRunId'] = failed_run_id
    if settings.data_backend != 'database' or not settings.database_url:
        return {**base,'status':'blocked','errorCode':'MACRO_EXTENSION_DATABASE_REQUIRED'}
    def validate_config(factory):
        config = load_extension_config()
        with factory() as session:
            row = session.get(JobRun,failed_run_id)
            d = FiscalStore(factory)._run(session,row)
            p = build_extension_plan('governmentBonds',config,datetime.fromisoformat(d['plan']['scheduledAt']))
            if p != d['plan']:
                raise ExtensionError('EXTENSION_POLICY_CHANGED')
    engine, lock, acquired = None, None, False
    report = {}
    try:
        url = make_url(settings.database_url)
        if url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg':
            return {**base,'status':'blocked','errorCode':'EXTENSION_POSTGRES_REQUIRED'}
        if dry_run:
            with open_readonly_factory(settings.database_url) as factory:
                validate_config(factory)
                return {**base,**FiscalStore(factory).replay_failed_raw(failed_run_id,dry_run=True)}
        engine,factory = create_engine_and_session(settings.database_url)
        lock = job_lock_for_engine(engine); acquired = lock.try_acquire(LOCK_KEY)
        if not acquired:
            report.update({**base,'status':'locked'})
            return report
        validate_config(factory)
        base['readOnly'] = False  # 随后写事务结果不明时不能退回只读。
        report.update({**base,**FiscalStore(factory).replay_failed_raw(failed_run_id)})
        return report
    except (ValueError,SQLAlchemyError,OSError,RuntimeError,KeyError,TypeError):
        report.update({**base,'status':'blocked','errorCode':'FISCAL_RAW_REPLAY_FAILED'})
        return report
    finally:
        # 事务可能已提交；清理异常不能覆盖已知回执或把写入误报为只读。
        for stage, action in [('release', lambda: lock.release(LOCK_KEY) if acquired else None),
                              ('dispose', lambda: engine.dispose() if engine is not None else None)]:
            try:
                action()
            except Exception:
                report.setdefault('cleanupErrors', []).append(stage)
