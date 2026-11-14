"""官方财政原件的持久预留和Raw重放；不执行网络，不清失败根。"""
import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.ingest import utc
from app.macro.extensions.normalize import number
from app.macro.extensions.plan import canonical, digest, validate_extension_plan
from app.macro.extensions.store import FAILURE_CODES, instant
from app.macro.fiscal_official import parse_government_bond_table
from app.macro.fiscal_plan import SERIES_ID

JOB = 'macro-official-government-v1'
RAW_REPLAY_JOB = 'macro-official-government-raw-replay-v1'
RAW_REPLAY_VERSION = 'official-government-table-v2'
DATASET = 'macro-official:governmentBonds'
NAMESPACE = UUID('7ade9138-c736-51a9-a47b-e8cb9a9e1e82')
ERRORS = FAILURE_CODES | {'FISCAL_TABLE_INVALID'}


def run_id(plan, document):
    return str(uuid5(NAMESPACE, plan['scheduledAt'] + ':' + str(document['year'])))


def request_hash(plan, document):
    return digest({'planFingerprint': plan['fingerprint'], 'document': document})


def core_value(text):
    value = number(text)
    with localcontext() as context:
        context.prec = 80
        value = value.quantize(Decimal('0.0000000001'), rounding=ROUND_HALF_EVEN)
    if abs(value) >= Decimal('1e18'):
        raise ExtensionError('EXTENSION_EXECUTION_INVALID')
    return value


class FiscalStore:
    def __init__(self, factory):
        self.factory = factory

    def _root(self, session, plan):
        validate_extension_plan(plan)
        if plan['groupId'] != 'governmentBonds':
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        root = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == plan['scheduledAt']))
        if root is None or root.cursor_value != {'plan': plan}:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        return root

    def _run(self, session, row, *, validate_bindings=True):
        if row is None:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        d = row.details
        if type(d) is not dict or set(d) != {'schemaVersion', 'plan', 'document', 'phase', 'rawId', 'coreBindings'} or type(d['schemaVersion']) is not int or d['schemaVersion'] != 1:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        p, doc, phase = d['plan'], d['document'], d['phase']
        self._root(session, p)
        if (doc not in p['documents'] or row.id != run_id(p, doc) or row.job_name != JOB
            or row.business_key != f"government-bonds:{p['scheduledAt']}:{doc['year']}"
            or row.request_count != 1 or phase not in ('reserved', 'received', 'completed', 'failed')
            or type(d['coreBindings']) is not list or row.row_count < 0):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if phase in ('reserved', 'received'):
            if row.status != 'running' or row.finished_at is not None or row.error_message is not None:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        elif (row.status != phase or row.finished_at is None or utc(row.finished_at) < utc(row.started_at)
            or (row.error_message not in ERRORS if phase == 'failed' else row.error_message is not None)):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if phase == 'reserved' and (d['rawId'] is not None or row.row_count):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if phase != 'completed' and (d['coreBindings'] or row.row_count):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        batch = session.get(IngestionBatch, row.id)
        if (batch is None or batch.source != 'pboc' or batch.dataset != DATASET or batch.request_count != 1
            or batch.row_count != row.row_count or utc(batch.started_at) != utc(row.started_at)
            or batch.status != {'reserved': 'running', 'received': 'processing', 'completed': 'completed', 'failed': 'failed'}[phase]
            or ((batch.finished_at is None) != (row.finished_at is None))
            or (batch.finished_at is not None and utc(batch.finished_at) != utc(row.finished_at))):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if phase in ('received', 'completed') or d['rawId'] is not None:
            self._raw(session, row, validate_bindings=validate_bindings)
        return d

    def _raw(self, session, row, *, validate_bindings=True):
        d, p, doc = row.details, row.details['plan'], row.details['document']
        if type(d['rawId']) is not int or d['rawId'] <= 0:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        raw = session.get(SourcePayload, d['rawId'])
        if raw is None or raw.batch_id != row.id or raw.api_name != 'pboc/government-bonds/html' or raw.request_hash != request_hash(p, doc):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        value = raw.payload
        if (type(value) is not dict or set(value) != {'schemaVersion', 'planFingerprint', 'document', 'bodyBase64', 'rawSha256', 'retrievedAt', 'releaseTimeBasis'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or type(value['bodyBase64']) is not str
            or len(value['bodyBase64']) > (p['limits']['maxResponseBytes'] + 2) // 3 * 4
            or value['planFingerprint'] != p['fingerprint'] or value['document'] != doc
            or value['releaseTimeBasis'] != 'retrieval-time-not-source-release' or digest(value) != raw.content_hash):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        try:
            body = base64.b64decode(value['bodyBase64'], validate=True)
            retrieved = instant(datetime.fromisoformat(value['retrievedAt']))
            if (not body or len(body) > p['limits']['maxResponseBytes'] or sha256(body).hexdigest() != value['rawSha256']
                or retrieved != utc(raw.captured_at) or retrieved < utc(row.started_at)):
                raise ValueError()
            if d['phase'] == 'completed' and validate_bindings:
                parsed = self._parse(p, doc, body)
                if len(parsed) != len(d['coreBindings']) or len(parsed) != row.row_count:
                    raise ValueError()
                self._check_bindings(session, row, body, retrieved, d['coreBindings'])
            return body, retrieved
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID') from None

    def _check_bindings(self, session, row, body, retrieved, bindings):
        parsed = self._parse(row.details['plan'], row.details['document'], body)
        if type(bindings) is not list or len(parsed) != len(bindings):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        for point, binding in zip(parsed, bindings, strict=True):
            if type(binding) is not dict or set(binding) != {'coreId', 'period', 'value', 'revision', 'releasedAt', 'originRawId'} or type(binding['coreId']) is not int:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            core = session.get(MacroObservation, binding['coreId'])
            if (core is None or core.source != 'pboc' or core.series_id != SERIES_ID or core.region != 'CN'
                or core.frequency != 'monthly' or core.unit != '亿元' or core.seasonal_adjustment != 'NSA'
                or core.source_url != row.details['document']['url'] or core.observed_at.isoformat() != point['period']
                or core.value != core_value(point['value']) or any(binding[k] != v for k, v in {'coreId': core.id,
                    'period': point['period'], 'value': str(core.value), 'revision': core.revision, 'releasedAt': utc(core.released_at).isoformat()}.items())
                or utc(core.released_at) > retrieved or (core.valid_until is not None and utc(core.valid_until) <= retrieved)):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            self._origin(session, core, binding)

    def raw_replay_receipt(self, session, row, *, validate_bindings=True):
        cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == RAW_REPLAY_JOB, JobCursor.cursor_key == row.id))
        if cursor is None:
            return None
        d = row.details
        if d['phase'] != 'failed' or row.error_message != 'FISCAL_TABLE_INVALID' or d['rawId'] is None:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        value = cursor.cursor_value
        names = {'schemaVersion','sourceRunId','rawId','contentHash','planFingerprint','normalizerVersion','completedAt','coreBindings','receiptHash'}
        raw = session.get(SourcePayload, d['rawId'])
        if (type(value) is not dict or set(value) != names or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['sourceRunId'] != row.id or type(value['rawId']) is not int or value['rawId'] != d['rawId']
            or value['contentHash'] != raw.content_hash or value['planFingerprint'] != d['plan']['fingerprint']
            or value['normalizerVersion'] != RAW_REPLAY_VERSION or type(value['coreBindings']) is not list
            or digest({k:v for k,v in value.items() if k != 'receiptHash'}) != value['receiptHash']):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        completed = instant(datetime.fromisoformat(value['completedAt']))
        body, retrieved = self._raw(session, row, validate_bindings=False)
        if (completed.isoformat() != value['completedAt'] or completed < utc(row.finished_at) or completed < retrieved
            or completed != utc(cursor.updated_at)):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if validate_bindings:
            self._check_bindings(session, row, body, retrieved, value['coreBindings'])
        return value

    def effective_phase(self, session, row, as_of):
        receipt = self.raw_replay_receipt(session, row)
        return 'completed' if receipt is not None and instant(datetime.fromisoformat(receipt['completedAt'])) <= instant(as_of) else row.details['phase']

    def _origin(self, session, core, binding):
        if type(binding['originRawId']) is not int:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        raw = session.get(SourcePayload, binding['originRawId'])
        origin = session.get(JobRun, raw.batch_id) if raw else None
        d = self._run(session, origin, validate_bindings=False)
        receipt = self.raw_replay_receipt(session, origin, validate_bindings=False)
        origin_bindings = receipt['coreBindings'] if receipt else d['coreBindings']
        if (d['phase'] != 'completed' and receipt is None) or d['rawId'] != raw.id or binding not in origin_bindings:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        body, retrieved = self._raw(session, origin, validate_bindings=False)
        points = self._parse(d['plan'], d['document'], body)
        expected = next((p for p in points if p['period'] == binding['period']), None)
        if (expected is None or core.value != core_value(expected['value']) or utc(core.released_at) != retrieved
            or binding['releasedAt'] != retrieved.isoformat() or core.source_url != d['document']['url']
            or core.revision != digest({'raw': sha256(body).hexdigest(), 'period': expected['period']})):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')

    @staticmethod
    def _parse(plan, document, body):
        parsed = parse_government_bond_table(body, document['year'])
        if len(parsed['points']) > plan['limits']['maxResponseRows']:
            raise ValueError()
        return [p for p in parsed['points'] if plan['startPeriod'] <= p['period'] <= plan['endPeriod']]

    def inspect(self, plan, now):
        now = instant(now)
        validate_extension_plan(plan)
        if instant(datetime.fromisoformat(plan['scheduledAt'])) > now:
            raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
        with self.factory() as session:
            roots = session.scalars(select(JobCursor).where(JobCursor.job_name == JOB).order_by(JobCursor.cursor_key)).all()
            rows = session.scalars(select(JobRun).where(JobRun.job_name == JOB).order_by(JobRun.started_at, JobRun.id)).all()
            by_root = {}
            phases = {}
            for root in roots:
                if type(root.cursor_value) is not dict or set(root.cursor_value) != {'plan'}:
                    raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                old = root.cursor_value['plan']; self._root(session, old)
                if root.cursor_key != old['scheduledAt'] or instant(datetime.fromisoformat(old['scheduledAt'])) > now:
                    raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                if old['groupPolicyHash'] != plan['groupPolicyHash']:
                    from app.macro.extensions.config import Schedule
                    from app.macro.extensions.plan import scheduled_due
                    previous_cycle = scheduled_due(Schedule.model_validate(old['groupPolicy']['schedule']), now)
                    if previous_cycle.isoformat() == old['scheduledAt']:
                        raise ExtensionError('EXTENSION_POLICY_CHANGED')
                by_root[root.cursor_key] = (old, [])
            for row in rows:
                d = self._run(session, row)
                phases[row.id] = self.effective_phase(session, row, now)
                if utc(row.started_at) > now or (row.finished_at is not None and utc(row.finished_at) > now):
                    raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                by_root[d['plan']['scheduledAt']][1].append(row)
            if any(not jobs for _, jobs in by_root.values()):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            chosen, jobs = plan, []
            if plan['scheduledAt'] in by_root:
                chosen, jobs = by_root[plan['scheduledAt']]
                if chosen != plan:
                    raise ExtensionError('EXTENSION_POLICY_CHANGED')
            for old, old_rows in by_root.values():
                if len(old_rows) < len(old['documents']) or any(phases[r.id] != 'completed' for r in old_rows):
                    if old['groupPolicyHash'] != plan['groupPolicyHash']:
                        raise ExtensionError('EXTENSION_POLICY_CHANGED')
                    chosen, jobs = old, old_rows
                    break
            states = []
            for doc in chosen['documents']:
                match = [r for r in jobs if r.details['document'] == doc]
                if len(match) > 1:
                    raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                row = match[0] if match else None
                states.append({'document': doc, 'status': phases[row.id] if row else 'new', 'runId': row.id if row else None,
                    'errorCode': row.error_message if row and phases[row.id] != 'completed' else None})
            if len(jobs) > chosen['limits']['maxHttpPerPeriod']:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            status = 'blocked' if any(s['status'] in ('reserved', 'failed') for s in states) else 'completed' if all(s['status'] == 'completed' for s in states) else 'partial' if jobs else 'new'
            return {'status': status, 'runId': chosen['fingerprint'] if jobs else None, 'plan': chosen,
                'documents': states, 'reservedCount': len(jobs)}

    def reserve(self, plan, document, now):
        now = instant(now)
        state = self.inspect(plan, now)
        if state['plan'] != plan or state['status'] == 'blocked' or document not in plan['documents']:
            raise ExtensionError('EXTENSION_ALREADY_RESERVED')
        try:
            with self.factory.begin() as session:
                root = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == plan['scheduledAt']).with_for_update())
                if root is None:
                    session.add(JobCursor(job_name=JOB, cursor_key=plan['scheduledAt'], cursor_value={'plan': plan})); session.flush()
                elif root.cursor_value != {'plan': plan}:
                    raise ExtensionError('EXTENSION_POLICY_CHANGED')
                if session.get(JobRun, run_id(plan, document)) is not None:
                    raise ExtensionError('EXTENSION_ALREADY_RESERVED')
                recent = session.scalars(select(JobRun).where(JobRun.job_name == JOB)).all()
                if sum(r.details['plan']['scheduledAt'] == plan['scheduledAt'] for r in recent) >= plan['limits']['maxHttpPerPeriod']:
                    raise ExtensionError('EXTENSION_RATE_LIMITED')
                if sum(utc(r.started_at) > now - timedelta(seconds=60) for r in recent) >= plan['limits']['requestsPerMinute']:
                    raise ExtensionError('EXTENSION_RATE_LIMITED')
                key = run_id(plan, document)
                session.add(JobRun(id=key, job_name=JOB, business_key=f"government-bonds:{plan['scheduledAt']}:{document['year']}",
                    status='running', started_at=now, request_count=1, row_count=0,
                    details={'schemaVersion': 1, 'plan': plan, 'document': document, 'phase': 'reserved', 'rawId': None, 'coreBindings': []}))
                session.add(IngestionBatch(id=key, source='pboc', dataset=DATASET, mode='bounded', business_date=now,
                    status='running', started_at=now, request_count=1, row_count=0, rejected_count=0))
            return key
        except IntegrityError:
            raise ExtensionError('EXTENSION_RESERVATION_CONFLICT') from None

    def receive(self, key, body, now):
        now = instant(now)
        with self.factory.begin() as session:
            row = session.get(JobRun, key, with_for_update=True); d = self._run(session, row)
            if d['phase'] != 'reserved' or type(body) is not bytes or not body or len(body) > d['plan']['limits']['maxResponseBytes'] or now < utc(row.started_at):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            value = {'schemaVersion': 1, 'planFingerprint': d['plan']['fingerprint'], 'document': d['document'],
                'bodyBase64': base64.b64encode(body).decode('ascii'), 'rawSha256': sha256(body).hexdigest(),
                'retrievedAt': now.isoformat(), 'releaseTimeBasis': 'retrieval-time-not-source-release'}
            raw = SourcePayload(batch_id=key, api_name='pboc/government-bonds/html', request_hash=request_hash(d['plan'], d['document']),
                content_hash=digest(value), payload=value, captured_at=now)
            session.add(raw); session.flush()
            row.details = {**d, 'phase': 'received', 'rawId': raw.id}
            session.get(IngestionBatch, key).status = 'processing'

    def fail(self, key, code, now):
        now = instant(now)
        if code not in ERRORS:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        with self.factory.begin() as session:
            row = session.get(JobRun, key, with_for_update=True); d = self._run(session, row)
            if d['phase'] not in ('reserved', 'received') or now < utc(row.started_at):
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            row.details = {**d, 'phase': 'failed'}
            row.status, row.finished_at, row.error_message = 'failed', now, code
            batch = session.get(IngestionBatch, key); batch.status, batch.finished_at = 'failed', now

    def replay_failed_raw(self, key, *, clock=None, dry_run=False):
        return self.replay(key, clock=clock, _failed_raw=True, dry_run=dry_run)

    def replay(self, key, *, clock=None, _failed_raw=False, dry_run=False):
        if type(_failed_raw) is not bool or type(dry_run) is not bool:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        with self.factory.begin() as session:
            row = session.get(JobRun, key, with_for_update=not dry_run); d = self._run(session, row)
            if _failed_raw:
                if d['phase'] != 'failed' or row.error_message != 'FISCAL_TABLE_INVALID' or d['rawId'] is None:
                    raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                checked = instant(clock() if clock else datetime.now(timezone.utc))
                if checked < utc(row.finished_at):
                    raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                existing_receipt = self.raw_replay_receipt(session, row)
                if existing_receipt is not None:
                    if instant(datetime.fromisoformat(existing_receipt['completedAt'])) > checked:
                        raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                    return {'status':'completed','readOnly':True,'supplierHttp':0,'insertedRows':0,'boundRows':len(existing_receipt['coreBindings']), 'sourceRunId':key}
            if d['phase'] == 'completed':
                return {'insertedRows': 0, 'boundRows': row.row_count}
            if d['phase'] != 'received' and not _failed_raw:
                raise ExtensionError('EXTENSION_EXECUTION_INVALID')
            body, retrieved = self._raw(session, row)
            try:
                points = self._parse(d['plan'], d['document'], body)
                values = [core_value(p['value']) for p in points]
            except (ValueError, TypeError, KeyError, ArithmeticError):
                raise ExtensionError('FISCAL_TABLE_INVALID') from None
            if dry_run:
                return {'status':'preview','readOnly':True,'supplierHttp':0,'insertedRows':0,'boundRows':len(points),'sourceRunId':key}
            bindings, inserted = [], 0
            for point, value in zip(points, values, strict=True):
                existing = session.scalars(select(MacroObservation).where(MacroObservation.series_id == SERIES_ID,
                    MacroObservation.observed_at == date.fromisoformat(point['period'])).with_for_update()).all()
                if any(r.source != 'pboc' or utc(r.released_at) > retrieved for r in existing):
                    raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                current = [r for r in existing if r.valid_until is None]
                if len(current) > 1 or (existing and not current):
                    raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                core = current[0] if current else None
                if core is not None and (core.region != 'CN' or core.frequency != 'monthly' or core.unit != '亿元' or core.seasonal_adjustment != 'NSA'):
                    raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                if core is None or core.value != value or core.source_url != d['document']['url']:
                    if core is not None:
                        if utc(core.released_at) >= retrieved:
                            raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                        core.valid_until = retrieved
                    core = MacroObservation(source='pboc', series_id=SERIES_ID, name='政府债券净融资', region='CN',
                        frequency='monthly', unit='亿元', seasonal_adjustment='NSA', observed_at=date.fromisoformat(point['period']),
                        released_at=retrieved, value=value, source_url=d['document']['url'], revision=digest({'raw': sha256(body).hexdigest(), 'period': point['period']}))
                    session.add(core); session.flush(); inserted += 1
                    binding = {'coreId': core.id, 'period': point['period'], 'value': str(core.value), 'revision': core.revision,
                        'releasedAt': retrieved.isoformat(), 'originRawId': d['rawId']}
                else:
                    prior_jobs = session.scalars(select(JobRun).where(JobRun.job_name == JOB, JobRun.status == 'completed').order_by(JobRun.started_at, JobRun.id)).all()
                    prior = next((b for prior_job in prior_jobs for b in prior_job.details.get('coreBindings', []) if b.get('coreId') == core.id), None)
                    if prior is None:
                        receipts = session.scalars(select(JobCursor).where(JobCursor.job_name == RAW_REPLAY_JOB)).all()
                        prior = next((b for receipt in receipts for b in receipt.cursor_value.get('coreBindings', []) if b.get('coreId') == core.id), None)
                    if prior is None:
                        raise ExtensionError('EXTENSION_EXECUTION_INVALID')
                    self._origin(session, core, prior)
                    binding = dict(prior)
                bindings.append(binding)
            finished = instant(clock() if clock else datetime.now(timezone.utc))
            if finished < retrieved:
                raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
            if _failed_raw:
                if finished < checked or finished < utc(row.finished_at):
                    raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                raw = session.get(SourcePayload, d['rawId'])
                receipt = {'schemaVersion':1,'sourceRunId':key,'rawId':raw.id,'contentHash':raw.content_hash,
                    'planFingerprint':d['plan']['fingerprint'],'normalizerVersion':RAW_REPLAY_VERSION,
                    'completedAt':finished.isoformat(),'coreBindings':bindings}
                session.add(JobCursor(job_name=RAW_REPLAY_JOB,cursor_key=key,cursor_value={**receipt,'receiptHash':digest(receipt)},updated_at=finished))
                return {'status':'completed','readOnly':False,'supplierHttp':0,'insertedRows':inserted,'boundRows':len(bindings),'sourceRunId':key}
            row.details = {**d, 'phase': 'completed', 'coreBindings': bindings}
            row.status, row.finished_at, row.row_count = 'completed', finished, len(bindings)
            batch = session.get(IngestionBatch, key); batch.status, batch.finished_at, batch.row_count = 'completed', finished, len(bindings)
            return {'insertedRows': inserted, 'boundRows': len(bindings)}
