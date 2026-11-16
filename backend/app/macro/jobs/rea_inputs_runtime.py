"""REA手动执行门：正式schema、只读预检、独立锁；无自动迁移/发布。"""
from datetime import UTC, datetime
import re
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.models.enterprise_rea_inputs import ReaInputPlan, ReaInputAttempt, ReaInputResponse, ReaInputSupplement
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.receivables_runtime import _structure_errors
from app.macro.jobs.rea_inputs_batch import prepare_rea_plan, read_rea_progress, execute_rea_inputs_batch
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_repository import load_enterprise_inputs, resolve_enterprise_selection
from app.macro.rea_input_preview import SOURCES, preview_rea_inputs
from app.macro.rea_input_store import EVIDENCE_ERRORS
from app.shared.readonly_database import open_readonly_factory

MODELS = (ReaInputPlan, ReaInputAttempt, ReaInputResponse, ReaInputSupplement)
LOCK_KEY = 'enterprise-rea-inputs'


def rea_input_schema(factory):
    problems = []
    with factory() as session:
        connection = session.connection()
        inspector = inspect(connection)
        for model in MODELS:
            table = model.__table__
            schema = table.schema
            if not inspector.has_table(table.name, schema=schema):
                problems.append(table.fullname)
            else:
                problems.extend(_structure_errors(inspector, table))
                if model is ReaInputPlan and not any(
                        item['column_names'] == ['sequence_number']
                        for item in inspector.get_unique_constraints(table.name, schema=schema)):
                    problems.append(table.fullname + ':sequence-unique')
        if not problems:
            rows = session.execute(text("""SELECT n.nspname || '.' || c.relname,p.prosrc
                FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
                JOIN pg_language lang ON lang.oid=p.prolang
                WHERE t.tgname='immutable_enterprise_rea' AND t.tgtype=27 AND t.tgqual IS NULL
                AND t.tgnargs=0 AND NOT t.tgisinternal AND t.tgconstraint=0 AND t.tgenabled IN ('O','A')
                AND pn.nspname='ops' AND p.proname='reject_enterprise_rea_mutation' AND p.pronargs=0
                AND NOT p.prosecdef AND p.prorettype='trigger'::regtype AND lang.lanname='plpgsql'"""))
            guarded = {table for table, body in rows if re.sub(r'\s+', ' ', body).strip()
                       == "BEGIN RAISE EXCEPTION 'ENTERPRISE_REA_IMMUTABLE'; END"}
            for model in (ReaInputPlan, ReaInputResponse, ReaInputSupplement):
                if model.__table__.fullname not in guarded:
                    problems.append(model.__table__.fullname + ':immutability-guard')
    return problems


def _capture(settings, instant):
    policy = EnterprisePolicy(settings.macro_enterprise_min_coverage, settings.macro_enterprise_min_samples,
                              settings.macro_enterprise_max_quarters)
    as_of = instant.astimezone(ZoneInfo(settings.sync_timezone)).date()
    with open_readonly_factory(settings.database_url) as factory:
        selection = resolve_enterprise_selection(factory, settings)
        inputs = load_enterprise_inputs(factory, selection)
        plan = preview_rea_inputs(inputs, as_of, policy, settings.macro_enterprise_supplement_quarters,
                                  settings.macro_catchup_request_limit)
        missing = rea_input_schema(factory)
        progress = {'processingCompleted': False, 'pendingCount': plan['logicalRequestCount'], 'replayPendingCount': 0,
                    'matchedCount': 0, 'unavailableCount': 0, 'blockedReasons': []}
        error = None
        if not missing:
            try:
                prepared = prepare_rea_plan(factory, inputs, as_of, policy, settings.macro_enterprise_supplement_quarters,
                                           settings.macro_catchup_request_limit, settings.tushare_page_size)
                plan = prepared['plan']
                progress = prepared.get('progress') or read_rea_progress(factory, plan, prepared['sources'], settings.tushare_page_size)
            except ValueError as exc:
                if str(exc) not in EVIDENCE_ERRORS:
                    raise
                error = str(exc)
        summary = {'schemaReady': not missing, 'missingTables': missing, 'poolVersion': plan['poolVersion'],
                   'poolCount': plan['poolCount'], 'planFingerprint': plan['planFingerprint'],
                   'logicalRequestCount': plan['logicalRequestCount'], 'outputPeriods': plan['outputPeriods'],
                   'frozenAsOfDate': plan['asOfDate'], **progress, **({'errorCode': error} if error else {})}
    return inputs, policy, as_of, summary


def run_enterprise_rea_inputs(settings, *, dry_run=False, probe=False, source=None, transport=None, now=None):
    base = {'requestCount': 0, 'responseCount': 0, 'readOnly': dry_run, 'publicationRequested': False,
            'operation': 'probe' if probe else 'batch', 'configuredHttpLimit': settings.macro_catchup_request_limit,
            'httpLimit': min(1, settings.macro_catchup_request_limit) if probe else settings.macro_catchup_request_limit,
            **({'probeReady': False, 'probeSource': source or 'balance'} if probe else {})}
    def blocked(code):
        return {**base, 'status': 'blocked', 'errorCode': code}
    if (source is not None and source not in SOURCES) or (source is not None and not probe):
        return blocked('REA_SOURCE_INVALID')
    if settings.data_backend != 'database' or not settings.database_url:
        return blocked('ENTERPRISE_DATABASE_REQUIRED')
    instant = now or datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError('REA_AWARE_TIME_REQUIRED')
    url = make_url(settings.database_url)
    if not dry_run and (url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg'):
        return blocked('ENTERPRISE_POSTGRES_REQUIRED')
    captured = _capture(settings, instant)
    summary = captured[3]
    if summary.get('errorCode') or summary['blockedReasons']:
        return {**base, **summary, 'status': 'blocked'}
    if dry_run:
        return {**base, **summary, 'status': 'preview'}
    if not summary['schemaReady']:
        return {**blocked('REA_SUPPLEMENT_SCHEMA_REQUIRED'), **summary}
    token = settings.tushare_token.get_secret_value() if settings.tushare_token else ''
    if not token:
        return blocked('ENTERPRISE_TUSHARE_TOKEN_REQUIRED')
    engine, factory = create_engine_and_session(settings.database_url)
    lock = job_lock_for_engine(engine)
    acquired = False
    try:
        acquired = lock.try_acquire(LOCK_KEY)
        if not acquired:
            return {**base, 'status': 'locked'}
        inputs, policy, as_of, summary = _capture(settings, instant)
        if summary.get('errorCode') or summary['blockedReasons']:
            return {**base, **summary, 'status': 'blocked'}
        if not summary['schemaReady']:
            return {**blocked('REA_SUPPLEMENT_SCHEMA_REQUIRED'), **summary}
        result = execute_rea_inputs_batch(factory, inputs, as_of, policy,
            quarters=settings.macro_enterprise_supplement_quarters, request_limit=settings.macro_catchup_request_limit,
            page_size=settings.tushare_page_size, token=token, transport=transport,
            rate_limiter=RequestRateLimiter(settings.tushare_standard_requests_per_minute), probe=probe, source=source)
        return {**base, **result, 'schemaReady': True}
    finally:
        if acquired:
            lock.release(LOCK_KEY)
        engine.dispose()
