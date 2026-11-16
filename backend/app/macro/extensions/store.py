"""现有Ops唯一键上的持久预算；无DDL，无网络，不重置失败根。"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.ingest import utc, validate_binding
from app.macro.extensions.normalize import normalize_extension_response, validate_evidence
from app.macro.extensions.plan import canonical, digest, validate_extension_plan


JOB_NAME = 'macro-extension-v1'
RECOVERY_JOB_NAME = 'macro-extension-recovery-v1'
NAMESPACE = UUID('607c0f50-b5af-4f9c-ae48-1cdd531df88c')
RECOVERY_NAMESPACE = UUID('b6a6bd3c-192a-418c-a97b-3c43ec6507a2')
FAILURE_CODES = frozenset('EXTENSION_' + code for code in (
    'AUTH_ERROR', 'PERMISSION_ERROR', 'RATE_LIMITED', 'NETWORK_ERROR', 'REDIRECT_REFUSED',
    'RESPONSE_INVALID', 'RESPONSE_TOO_LARGE', 'HTTP_ERROR', 'TIMEOUT', 'SERVER_ERROR',
    'ENCODING_UNSUPPORTED', 'COLLECTION_CHANGED', 'PROVIDER_REJECTED', 'EXECUTION_FAILED'))


def instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExtensionError('EXTENSION_EXECUTION_INVALID')
    return value.astimezone(timezone.utc)


def run_key(plan: dict) -> str:
    due = instant(datetime.fromisoformat(plan['scheduledAt'])).isoformat()
    return str(uuid5(NAMESPACE, f"{plan['groupId']}:{due}"))


def business_key(plan: dict) -> str:
    return f"macro-extension:{plan['groupId']}:{plan['scheduledAt']}"


def recovery_key(parent_id: str) -> str:
    return str(uuid5(RECOVERY_NAMESPACE, parent_id))


def recovery_business_key(plan: dict, parent_id: str) -> str:
    return f"macro-extension-recovery:{plan['groupId']}:{parent_id}"


def execution_schema(factory) -> list[str]:
    with factory() as session:
        connection = session.connection()
        inspector = inspect(connection)
        problems = []
        for model in (JobCursor, JobRun, IngestionBatch, SourcePayload, MacroObservation):
            table = model.__table__
            schema = table.schema
            if not inspector.has_table(table.name, schema=schema):
                problems.append(f'{table.schema}.{table.name}')
            elif not set(table.columns.keys()) <= {column['name'] for column in inspector.get_columns(table.name, schema=schema)}:
                problems.append(f'{table.schema}.{table.name}:columns')
            else:
                if set(inspector.get_pk_constraint(table.name, schema=schema)['constrained_columns']) != set(table.primary_key.columns.keys()):
                    problems.append(f'{table.schema}.{table.name}:primary-key')
                if model in (JobCursor, SourcePayload):
                    expected = {'job_name', 'cursor_key'} if model is JobCursor else {'batch_id', 'request_hash'}
                    if not any(set(item['column_names']) == expected for item in inspector.get_unique_constraints(table.name, schema=schema)):
                        problems.append(f'{table.schema}.{table.name}:unique-key')
        return problems


def _job(row: JobRun, group_id: str) -> dict:
    details = row.details
    try:
        recovery = row.job_name == RECOVERY_JOB_NAME
        keys = {'schemaVersion', 'plan', 'phase', 'rawId', 'coreBindings'} | ({'recoveryOf'} if recovery else set())
        if type(details) is not dict or set(details) != keys or type(details['schemaVersion']) is not int or details['schemaVersion'] != 1:
            raise ValueError()
        plan = details['plan']
        validate_extension_plan(plan)
        parent = run_key(plan)
        if recovery and details['recoveryOf'] != parent:
            raise ValueError()
        expected_id = recovery_key(parent) if recovery else parent
        expected_business = recovery_business_key(plan, parent) if recovery else business_key(plan)
        phase = details['phase']
        if (plan['groupId'] != group_id or row.id != expected_id or row.business_key != expected_business
            or row.job_name not in (JOB_NAME, RECOVERY_JOB_NAME) or row.request_count != 1 or phase not in ('reserved', 'received', 'completed', 'failed')
            or type(details['coreBindings']) is not list or row.row_count < 0):
            raise ValueError()
        if phase in ('reserved', 'received'):
            if row.status != 'running' or row.finished_at is not None or row.error_message is not None:
                raise ValueError()
        elif row.status != phase or row.finished_at is None or utc(row.finished_at) < utc(row.started_at):
            raise ValueError()
        if phase == 'failed':
            if row.error_message not in FAILURE_CODES:
                raise ValueError()
        elif row.error_message is not None:
            raise ValueError()
        if phase in ('reserved', 'failed'):
            if details['rawId'] is not None or details['coreBindings'] or row.row_count != 0:
                raise ValueError()
        elif type(details['rawId']) is not int or details['rawId'] <= 0:
            raise ValueError()
        if phase != 'completed' and details['coreBindings']:
            raise ValueError()
        return details
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ExtensionError('EXTENSION_EXECUTION_INVALID') from None


class ExtensionStore:
    def __init__(self, factory):
        self.factory = factory

    def attempt_key(self, plan):
        return run_key(plan)

    def _parent(self, session, row):
        if row.job_name != RECOVERY_JOB_NAME:
            return
        details = _job(row, row.details['plan']['groupId'])
        parent = session.get(JobRun, details['recoveryOf'])
        if parent is None or parent.job_name != JOB_NAME:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        previous = _job(parent, details['plan']['groupId'])
        if (previous['phase'] != 'failed' or parent.error_message != 'EXTENSION_RESPONSE_INVALID'
            or canonical(previous['plan']) != canonical(details['plan'])
            or utc(row.started_at) < utc(parent.finished_at)):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')

    def _root(self, session, plan):
        root = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME, JobCursor.cursor_key == plan['groupId']))
        if root is None or type(root.cursor_value) is not dict or set(root.cursor_value) != {'plan'}:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        first = root.cursor_value['plan']
        validate_extension_plan(first)
        if first['groupId'] != plan['groupId'] or first['groupPolicyHash'] != plan['groupPolicyHash']:
            raise ExtensionError('EXTENSION_POLICY_CHANGED')
        initial = session.get(JobRun, run_key(first))
        if initial is None or canonical(_job(initial, plan['groupId'])['plan']) != canonical(first):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')

    def _raw(self, session, row, *, root_checked=False):
        details = _job(row, row.details['plan']['groupId'])
        self._parent(session, row)
        if not root_checked:
            self._root(session, details['plan'])
        raw = session.get(SourcePayload, details['rawId'])
        batch = session.get(IngestionBatch, row.id)
        if raw is None or batch is None:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        value = raw.payload
        try:
            if type(value) is not dict or set(value) != {'schemaVersion', 'plan', 'evidence', 'retrievedAt', 'releaseTimeBasis'}:
                raise ValueError()
            retrieved = instant(datetime.fromisoformat(value['retrievedAt']))
            validate_evidence(value['evidence'])
            if (type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or canonical(value['plan']) != canonical(details['plan'])
                or canonical(value['evidence']['plan']) != canonical(details['plan'])
                or value['releaseTimeBasis'] != 'retrieval-time-not-source-release'
                or raw.batch_id != row.id or raw.api_name != details['plan']['query']['api']
                or raw.request_hash != details['plan']['fingerprint'] or raw.content_hash != digest(value)
                or utc(raw.captured_at) != retrieved or retrieved < utc(row.started_at)
                or batch.source != details['plan']['source'] or batch.dataset != f"macro-extension:{details['plan']['groupId']}"
                or batch.request_count != 1 or row.row_count != len(value['evidence']['observations'])
                or batch.row_count != row.row_count):
                raise ValueError()
            if details['phase'] == 'completed':
                if len(details['coreBindings']) != len(value['evidence']['observations']):
                    raise ValueError()
                for observation, binding in zip(value['evidence']['observations'], details['coreBindings'], strict=True):
                    validate_binding(session, details['plan'], observation, binding, retrieved)
            return value, retrieved
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID') from None

    def _inspect(self, session, plan, now):
        validate_extension_plan(plan)
        now = instant(now)
        if instant(datetime.fromisoformat(plan['scheduledAt'])) > now:
            raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
        root = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME, JobCursor.cursor_key == plan['groupId']))
        rows = session.scalars(select(JobRun).where(JobRun.job_name == JOB_NAME,
            JobRun.business_key.startswith(f"macro-extension:{plan['groupId']}:"))).all()
        recoveries = session.scalars(select(JobRun).where(JobRun.job_name == RECOVERY_JOB_NAME,
            JobRun.business_key.startswith(f"macro-extension-recovery:{plan['groupId']}:"))).all()
        recovered = {}
        for child in recoveries:
            child_details = _job(child, plan['groupId'])
            self._parent(session, child)
            if (utc(child.started_at) > now or (child.finished_at is not None and utc(child.finished_at) > now)):
                raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
            if child_details['phase'] in ('received', 'completed'):
                self._raw(session, child)
            if not any(row.id == child_details['recoveryOf'] for row in rows):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            recovered[child_details['recoveryOf']] = child
        if root is None and rows:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if root is not None:
            value = root.cursor_value
            if type(value) is not dict or set(value) != {'plan'}:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            validate_extension_plan(value['plan'])
            if value['plan']['groupId'] != plan['groupId'] or value['plan']['groupPolicyHash'] != plan['groupPolicyHash']:
                raise ExtensionError('EXTENSION_POLICY_CHANGED')
            if not any(row.id == run_key(value['plan']) for row in rows):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            self._root(session, plan)
        # 保持强引用，使后续逐证据绑定校验走identity map，不逐点查询旧Core。
        retained = (
            session.scalars(select(SourcePayload).join(IngestionBatch).where(
                IngestionBatch.dataset == f"macro-extension:{plan['groupId']}")).all(),
            session.scalars(select(IngestionBatch).where(
                IngestionBatch.dataset == f"macro-extension:{plan['groupId']}")).all(),
            session.scalars(select(MacroObservation).where(MacroObservation.series_id == plan['seriesId'])).all(),
        )
        for core in retained[2]:
            if (core.source != plan['source'] or core.frequency != plan['frequency'] or core.unit != plan['unit']
                or core.source_url != plan['sourceUrl'] or core.region != plan.get('region', 'CN') or core.seasonal_adjustment != 'NSA'
                or not core.value.is_finite() or core.value <= 0
                or (core.valid_until is not None and utc(core.valid_until) <= utc(core.released_at))
                or (plan['groupId'] == 'nominalGdp' and (core.observed_at.day != 1 or core.observed_at.month not in (1, 4, 7, 10)))):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        current, pending = None, []
        for row in rows:
            details = _job(row, plan['groupId'])
            if details['phase'] in ('received', 'completed'):
                self._raw(session, row, root_checked=True)
            if details['plan']['groupPolicyHash'] != plan['groupPolicyHash']:
                raise ExtensionError('EXTENSION_POLICY_CHANGED')
            if utc(row.started_at) > now or instant(datetime.fromisoformat(details['plan']['scheduledAt'])) > instant(datetime.fromisoformat(plan['scheduledAt'])):
                raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
            effective = recovered.get(row.id, row)
            effective_details = _job(effective, plan['groupId'])
            if effective_details['phase'] != 'completed': pending.append(effective)
            if row.id == run_key(plan):
                if canonical(details['plan']) != canonical(plan):
                    raise ExtensionError('EXTENSION_POLICY_CHANGED')
                current = effective
        if len(pending) > 1:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        row = pending[0] if pending else current
        if row is not None:
            details = _job(row, plan['groupId'])
            return {'status': 'uncertain' if details['phase'] == 'reserved' else details['phase'],
                'runId': row.id, 'plan': details['plan'], 'reservedCount': 1, 'errorCode': row.error_message}
        return {'status': 'new', 'runId': None, 'plan': plan, 'reservedCount': 0, 'errorCode': None}

    def inspect(self, plan: dict, now: datetime) -> dict:
        with self.factory() as session:
            return self._inspect(session, plan, now)

    def reserve(self, plan: dict, now: datetime) -> str:
        now = instant(now)
        try:
            with self.factory.begin() as session:
                # 正式入口还持有PG会话锁；行锁保护已建立的组策略。
                root = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME,
                    JobCursor.cursor_key == plan['groupId']).with_for_update())
                state = self._inspect(session, plan, now)
                if state['status'] != 'new':
                    raise ExtensionError('EXTENSION_ALREADY_RESERVED')
                recent = session.scalars(select(JobRun).where(JobRun.job_name == JOB_NAME,
                    JobRun.business_key.startswith(f"macro-extension:{plan['groupId']}:"))).all()
                if sum(utc(row.started_at) > now - timedelta(seconds=60) for row in recent) >= plan['limits']['requestsPerMinute']:
                    raise ExtensionError('EXTENSION_RATE_LIMITED')
                if root is None:
                    session.add(JobCursor(job_name=JOB_NAME, cursor_key=plan['groupId'], cursor_value={'plan': plan}))
                key = run_key(plan)
                session.add(JobRun(id=key, job_name=JOB_NAME, business_key=business_key(plan), status='running',
                    started_at=now, request_count=1, row_count=0,
                    details={'schemaVersion': 1, 'plan': plan, 'phase': 'reserved', 'rawId': None, 'coreBindings': []}))
                session.add(IngestionBatch(id=key, source=plan['source'], dataset=f"macro-extension:{plan['groupId']}",
                    mode='bounded', business_date=now, status='running', started_at=now,
                    request_count=1, row_count=0, rejected_count=0))
            return key
        except IntegrityError:
            raise ExtensionError('EXTENSION_RESERVATION_CONFLICT') from None

    def receive(self, run_id: str, response: object, now: datetime) -> None:
        now = instant(now)
        with self.factory.begin() as session:
            row = session.get(JobRun, run_id, with_for_update=True)
            if row is None:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            details = _job(row, row.details['plan']['groupId'])
            self._parent(session, row)
            self._root(session, details['plan'])
            if details['phase'] != 'reserved' or now < utc(row.started_at):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            evidence = normalize_extension_response(details['plan'], response)
            value = {'schemaVersion': 1, 'plan': details['plan'], 'evidence': evidence,
                'retrievedAt': now.isoformat(), 'releaseTimeBasis': 'retrieval-time-not-source-release'}
            raw = SourcePayload(batch_id=row.id, api_name=details['plan']['query']['api'],
                request_hash=details['plan']['fingerprint'], content_hash=digest(value), payload=value, captured_at=now)
            session.add(raw)
            session.flush()
            row.row_count = len(evidence['observations'])
            row.details = {**details, 'phase': 'received', 'rawId': raw.id}
            batch = session.get(IngestionBatch, row.id)
            batch.row_count = row.row_count
            batch.status = 'processing'

    def fail(self, run_id: str, code: str, now: datetime) -> None:
        now = instant(now)
        if code not in FAILURE_CODES:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        with self.factory.begin() as session:
            row = session.get(JobRun, run_id, with_for_update=True)
            if row is None:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            details = _job(row, row.details['plan']['groupId'])
            self._parent(session, row)
            self._root(session, details['plan'])
            if details['phase'] != 'reserved' or now < utc(row.started_at):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            row.details = {**details, 'phase': 'failed'}
            row.status, row.error_message, row.finished_at = 'failed', code, now
            batch = session.get(IngestionBatch, row.id)
            batch.status, batch.finished_at = 'failed', now
            from app.acquisition.adapters.plans import record_extension
            record_extension(session,row,now)

    def replay(self, run_id: str, *, clock=None) -> dict:
        from app.macro.extensions.ingest import ingest_evidence
        with self.factory.begin() as session:
            row = session.get(JobRun, run_id, with_for_update=True)
            if row is None:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            details = _job(row, row.details['plan']['groupId'])
            if details['phase'] not in ('received', 'completed'):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            value, retrieved_at = self._raw(session, row)
            if details['phase'] == 'completed':
                return {'insertedRows': 0, 'boundRows': row.row_count}
            before = set(session.scalars(select(MacroObservation.id).where(MacroObservation.series_id == details['plan']['seriesId'])))
            bindings = ingest_evidence(session, value['evidence'], retrieved_at)
            completed_at = instant(clock() if clock is not None else datetime.now(timezone.utc))
            if completed_at < retrieved_at:
                raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
            row.details = {**details, 'phase': 'completed', 'coreBindings': bindings}
            row.status, row.finished_at = 'completed', completed_at
            batch = session.get(IngestionBatch, row.id)
            batch.status, batch.finished_at = 'completed', completed_at
            from app.acquisition.adapters.plans import record_extension
            record_extension(session,row,completed_at)
            return {'insertedRows': sum(binding['coreId'] not in before for binding in bindings), 'boundRows': len(bindings)}
