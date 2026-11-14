"""显式手动企业字段入口；未接入日常调度或发布。"""
from datetime import UTC, datetime
import re
from zoneinfo import ZoneInfo

from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.engine import make_url

from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.models.enterprise_receivables import ReceivableAttempt, ReceivableResponse, ReceivableSupplement
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.receivables_batch import execute_receivables_batch, read_receivables_progress
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_repository import load_enterprise_inputs, resolve_enterprise_selection
from app.macro.receivables_preview import preview_receivables
from app.macro.receivables_store import EVIDENCE_ERRORS
from app.macro.balance_supplement import RECEIVABLES, target_options
from app.shared.readonly_database import open_readonly_factory

LOCK_KEY = 'enterprise-receivables'


def _structure_errors(inspector, table):
    """正式迁移的必要约束；只读核验，不修表或接管同名用户对象。"""
    columns={c['name']:c for c in inspector.get_columns(table.name,schema=table.schema)}
    errors=[]
    if any(c.name not in columns or columns[c.name]['nullable']!=c.nullable
        or not isinstance(columns[c.name]['type'],type(c.type))
        or getattr(columns[c.name]['type'],'length',None)!=getattr(c.type,'length',None)
        or getattr(columns[c.name]['type'],'timezone',None)!=getattr(c.type,'timezone',None)
        for c in table.c): errors.append(table.fullname+':column-contract')
    if inspector.get_pk_constraint(table.name,schema=table.schema)['constrained_columns']!=[c.name for c in table.primary_key]:
        errors.append(table.fullname+':primary-key')
    actual={(tuple(f['constrained_columns']),f['referred_schema'],f['referred_table'],tuple(f['referred_columns']))
        for f in inspector.get_foreign_keys(table.name,schema=table.schema)}
    required={(tuple(e.parent.name for e in f.elements),f.elements[0].column.table.schema,
        f.elements[0].column.table.name,tuple(e.column.name for e in f.elements)) for f in table.foreign_key_constraints}
    if not required<=actual: errors.append(table.fullname+':foreign-key')
    def normalize(sql):
        return re.sub(r'\s+','',re.sub(r'::(?:character varying|text)(?:\[\])?','',sql)).lower()
    actual_checks={normalize(c['sqltext']) for c in inspector.get_check_constraints(table.name,schema=table.schema)}
    for check in (c for c in table.constraints if isinstance(c,CheckConstraint)):
        sql=str(check.sqltext)
        enum=re.fullmatch(r"(\w+) IN \((.+)\)",sql)
        # PostgreSQL将IN规范化为ANY ARRAY，并去掉此已知AND/OR式的冗余分组。
        expected=normalize(f'{enum[1]}=ANY(ARRAY[{enum[2]}])' if enum else sql.replace('(','').replace(')',''))
        if expected not in actual_checks: errors.append(table.fullname+':check-constraint')
    return errors


def _schema(factory, *, target=RECEIVABLES) -> list[str]:
    target_options(target)
    missing = []
    with factory() as session:
        connection = session.connection()
        inspector = inspect(connection)
        for model in target.models:
            table = model.__table__
            schema = None if connection.dialect.name == 'sqlite' else table.schema
            if not inspector.has_table(table.name, schema=schema):
                missing.append(table.fullname)
            elif not set(table.c.keys()) <= {col['name'] for col in inspector.get_columns(table.name, schema=schema)}:
                missing.append(table.fullname + ':columns')
            elif connection.dialect.name=='postgresql':
                missing.extend(_structure_errors(inspector,table))
        if not missing and connection.dialect.name == 'postgresql':
            # 同名空表或metadata.create_all不是正式迁移，缺少不可变保护则拒绝执行。
            guards = set()
            rows=session.execute(text("""SELECT n.nspname || '.' || c.relname,p.prosrc
                FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
                JOIN pg_language lang ON lang.oid=p.prolang
                WHERE t.tgname=:trigger_name AND t.tgtype=27 AND t.tgqual IS NULL
                AND t.tgnargs=0 AND NOT t.tgisinternal AND t.tgconstraint=0
                AND t.tgenabled IN ('O','A') AND pn.nspname='ops'
                AND p.proname=:function_name AND p.pronargs=0 AND NOT p.prosecdef
                AND p.prorettype='trigger'::regtype AND lang.lanname='plpgsql'"""),{'trigger_name':'immutable_'+target.table_prefix,
                    'function_name':'reject_'+target.table_prefix+'_mutation'})
            expected=f"BEGIN RAISE EXCEPTION '{target.table_prefix.upper()}_IMMUTABLE'; END"
            for table,body in rows:
                if re.sub(r'\s+',' ',body).strip()==expected: guards.add(table)
            for table in (target.models[1].__table__.fullname, target.models[2].__table__.fullname):
                if table not in guards:
                    missing.append(table + ':immutability-guard')
    return missing


def _capture(settings, instant, *, target=RECEIVABLES):
    options=target_options(target)
    policy = EnterprisePolicy(settings.macro_enterprise_min_coverage,
        settings.macro_enterprise_min_samples, settings.macro_enterprise_max_quarters)
    as_of = instant.astimezone(ZoneInfo(settings.sync_timezone)).date()
    with open_readonly_factory(settings.database_url) as factory:
        selection = resolve_enterprise_selection(factory, settings)
        inputs = load_enterprise_inputs(factory, selection)
        plan = preview_receivables(inputs, as_of, policy,
            settings.macro_enterprise_supplement_quarters, settings.macro_catchup_request_limit,**options)
        missing = _schema(factory,**options)
        if missing:
            progress = {'processingCompleted': False, 'matchedCount': 0, 'unavailableCount': 0,
                'pendingCount': len(plan['requests']), 'unavailableReasons': {},
                'unverifiedCount': 0, 'blockedReasons': []}
        else:
            try:
                progress = read_receivables_progress(factory, inputs, plan, settings.tushare_page_size,**options)
            except ValueError as exc:
                if str(exc) not in EVIDENCE_ERRORS:
                    raise
                progress = read_receivables_progress(factory, inputs, plan, settings.tushare_page_size, tolerate_invalid=True,**options)
                progress['errorCode'] = str(exc)
    summary = {'schemaReady': not missing, 'missingTables': missing,
        'planFingerprint': plan['planFingerprint'], 'poolVersion': selection.version, 'poolCount': len(selection.symbols),
        'logicalRequestCount': len(plan['requests']), 'outputPeriods': plan['outputPeriods'],
        'balancePeriods': plan['balancePeriods'], **progress}
    return inputs, policy, as_of, summary


def run_enterprise_receivables(settings, *, dry_run: bool = False, transport=None, now=None,
        probe: bool = False, target=RECEIVABLES) -> dict:
    options=target_options(target)
    lock_key=LOCK_KEY if target is RECEIVABLES else 'enterprise-orders'
    base = {'requestCount': 0, 'responseCount': 0, 'newMatchedCount': 0,
        'httpLimit': min(settings.macro_catchup_request_limit, 1) if probe else settings.macro_catchup_request_limit,
        'configuredHttpLimit': settings.macro_catchup_request_limit,
        'operation': 'probe' if probe else 'batch', 'readOnly': dry_run, 'publicationRequested': False,
        **({'probeReady': False} if probe else {})}
    def blocked(code):
        return {**base, 'status': 'blocked', 'errorCode': code}
    if settings.data_backend != 'database' or not settings.database_url:
        return blocked('ENTERPRISE_DATABASE_REQUIRED')
    instant = now if now is not None else datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError('ENTERPRISE_AWARE_TIME_REQUIRED')
    url = make_url(settings.database_url)
    if not dry_run and (url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg'):
        return blocked('ENTERPRISE_POSTGRES_REQUIRED')
    inputs, policy, as_of, summary = _capture(settings, instant,**options)
    if summary.get('errorCode'):
        return {**base, **summary, 'status': 'blocked'}
    if dry_run:
        return {**base, **summary, 'status': 'preview'}
    if not summary['schemaReady']:
        return {**blocked('ENTERPRISE_SUPPLEMENT_SCHEMA_REQUIRED'), **summary}
    token = settings.tushare_token.get_secret_value() if settings.tushare_token else ''
    if not token:
        return blocked('ENTERPRISE_TUSHARE_TOKEN_REQUIRED')
    engine, factory = create_engine_and_session(settings.database_url)
    lock = job_lock_for_engine(engine)
    acquired = False
    try:
        acquired = lock.try_acquire(lock_key)
        if not acquired:
            return {**base, 'status': 'locked'}
        # 丢弃锁外快照：取得专用跨进程锁后重新捕获当前源/规划/证据。
        inputs, policy, as_of, summary = _capture(settings, instant,**options)
        if summary.get('errorCode'):
            return {**base, **summary, 'status': 'blocked'}
        if not summary['schemaReady']:
            return {**blocked('ENTERPRISE_SUPPLEMENT_SCHEMA_REQUIRED'), **summary}
        result = execute_receivables_batch(factory, inputs, as_of, policy,
            quarters=settings.macro_enterprise_supplement_quarters,
            request_limit=settings.macro_catchup_request_limit, page_size=settings.tushare_page_size,
            token=token, transport=transport,
            rate_limiter=RequestRateLimiter(settings.tushare_standard_requests_per_minute), probe=probe,**options)
        return {**base, **result, 'schemaReady': True, 'poolVersion': summary['poolVersion']}
    finally:
        if acquired:
            lock.release(lock_key)
        engine.dispose()
