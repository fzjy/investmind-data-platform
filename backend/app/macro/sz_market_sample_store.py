"""固定项目级单槽，跨父/跨日不扩额；仅Raw，无Core重放入口。"""
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select, text

from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import digest
from app.macro.extensions.store import FAILURE_CODES, instant
from app.macro.sz_market_sample import build_sample_plan, safe_sample, sample_summary, validate_sample_plan

JOB = 'macro-sz-market-contract-sample-v1'
KEY = 'single-project-pilot'
RUN = str(uuid5(UUID('0fa970f7-cf09-48b3-9f9e-6657243e1515'), JOB + ':' + KEY))


class SzMarketSampleStore:
    def __init__(self, factory):
        self.factory = factory

    def _state(self, session, plan, now):
        p = validate_sample_plan(plan)
        if build_sample_plan(self.factory, p, now) != plan:
            raise ValueError('SZ_SAMPLE_PARENT_CHANGED')
        roots = session.scalars(select(JobCursor).where(JobCursor.job_name == JOB)).all()
        runs = session.scalars(select(JobRun).where(JobRun.job_name == JOB)).all()
        if not roots:
            if runs:
                raise ValueError('SZ_SAMPLE_ROOT_INVALID')
            return {'status': 'new', 'reservedCount': 0, 'summary': None}
        if (len(roots) != 1 or roots[0].cursor_key != KEY or len(runs) != 1
            or type(roots[0].cursor_value) is not dict or set(roots[0].cursor_value) != {'plan'}):
            raise ValueError('SZ_SAMPLE_ROOT_INVALID')
        frozen = roots[0].cursor_value['plan']
        validate_sample_plan(frozen)
        row = runs[0]
        d = row.details
        if (row.id != RUN or row.business_key != KEY or row.request_count != 1
            or type(d) is not dict or set(d) != {'plan', 'phase', 'rawId', 'summary'} or d['plan'] != frozen
            or d['phase'] not in ('reserved', 'failed', 'captured') or utc(row.started_at) > now
            or utc(roots[0].updated_at) != utc(row.started_at)):
            raise ValueError('SZ_SAMPLE_RUN_INVALID')
        finished = d['phase'] != 'reserved'
        if (row.status != {'reserved': 'running', 'failed': 'failed', 'captured': 'completed'}[d['phase']]
            or (row.finished_at is not None) != finished
            or finished and not utc(row.started_at) <= utc(row.finished_at) <= now
            or (row.error_message not in FAILURE_CODES if d['phase'] == 'failed' else row.error_message is not None)):
            raise ValueError('SZ_SAMPLE_RUN_INVALID')
        batch = session.get(IngestionBatch, RUN)
        if (batch is None or batch.source != 'tushare' or batch.dataset != JOB or batch.mode != 'live'
            or batch.request_count != 1 or batch.row_count != row.row_count or batch.rejected_count != 0
            or batch.status != row.status or utc(batch.started_at) != utc(row.started_at)
            or (batch.finished_at is not None) != finished
            or finished and utc(batch.finished_at) != utc(row.finished_at)):
            raise ValueError('SZ_SAMPLE_BATCH_INVALID')
        if d['phase'] == 'captured':
            if type(d['rawId']) is not int or d['rawId'] <= 0:
                raise ValueError('SZ_SAMPLE_RAW_INVALID')
            raw = session.get(SourcePayload, d['rawId'])
            if (raw is None or raw.batch_id != RUN or raw.api_name != 'sz_daily_info'
                or raw.request_hash != digest({'plan': frozen['fingerprint'], 'query': frozen['query']})
                or digest(raw.payload) != raw.content_hash):
                raise ValueError('SZ_SAMPLE_RAW_INVALID')
            value = raw.payload
            if (type(value) is not dict or set(value) != {'schemaVersion', 'planFingerprint', 'businessData',
                'businessSha256', 'retrievedAt', 'unitVerified', 'coreEligible'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or value['planFingerprint'] != frozen['fingerprint'] or value['unitVerified'] is not False
                or value['coreEligible'] is not False):
                raise ValueError('SZ_SAMPLE_RAW_INVALID')
            data = safe_sample(value['businessData'], frozen)
            if (data != value['businessData'] or digest(data) != value['businessSha256']
                or sample_summary(data, frozen) != d['summary'] or row.row_count != len(data['items'])
                or value['retrievedAt'] != utc(raw.captured_at).isoformat()
                or not utc(row.started_at) <= utc(raw.captured_at) <= utc(row.finished_at)):
                raise ValueError('SZ_SAMPLE_RAW_INVALID')
        elif d['rawId'] is not None or d['summary'] is not None or row.row_count:
            raise ValueError('SZ_SAMPLE_RUN_INVALID')
        if frozen != plan:
            return {'status': 'blocked', 'errorCode': 'SZ_SAMPLE_PLAN_FROZEN', 'reservedCount': 1, 'summary': None}
        return {'status': d['phase'], 'reservedCount': 1, 'summary': d['summary'],
            'errorCode': row.error_message, 'runId': RUN}

    def inspect(self, plan, now):
        with self.factory() as session:
            return self._state(session, plan, instant(now))

    def reserve(self, plan, now):
        now = instant(now)
        with self.factory.begin() as session:
            if session.bind.dialect.name == 'postgresql':
                lock = int.from_bytes(sha256(JOB.encode()).digest()[:8], 'big', signed=True)
                session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
            if self._state(session, plan, now)['status'] != 'new':
                raise ValueError('SZ_SAMPLE_RESERVATION_BLOCKED')
            session.add(JobCursor(job_name=JOB, cursor_key=KEY, cursor_value={'plan': plan}, updated_at=now))
            session.add(JobRun(id=RUN, job_name=JOB, business_key=KEY, status='running', started_at=now,
                request_count=1, row_count=0, details={'plan': plan, 'phase': 'reserved', 'rawId': None, 'summary': None}))
            session.add(IngestionBatch(id=RUN, source='tushare', dataset=JOB, mode='live', status='running',
                started_at=now, request_count=1, row_count=0, rejected_count=0))
        return RUN

    def finish(self, plan, now, *, data=None, error=None):
        now = instant(now)
        with self.factory.begin() as session:
            row = session.get(JobRun, RUN, with_for_update=True)
            if self._state(session, plan, now)['status'] != 'reserved':
                raise ValueError('SZ_SAMPLE_FINISH_INVALID')
            batch = session.get(IngestionBatch, RUN)
            if error is not None:
                if error not in FAILURE_CODES or data is not None:
                    raise ValueError('SZ_SAMPLE_FINISH_INVALID')
                row.details = {**row.details, 'phase': 'failed'}
                row.status = batch.status = 'failed'
                row.error_message = error
            else:
                data = safe_sample(data, plan)
                value = {'schemaVersion': 1, 'planFingerprint': plan['fingerprint'], 'businessData': data,
                    'businessSha256': digest(data), 'retrievedAt': now.isoformat(), 'unitVerified': False, 'coreEligible': False}
                raw = SourcePayload(batch_id=RUN, api_name='sz_daily_info',
                    request_hash=digest({'plan': plan['fingerprint'], 'query': plan['query']}),
                    content_hash=digest(value), payload=value, captured_at=now)
                session.add(raw)
                session.flush()
                row.details = {**row.details, 'phase': 'captured', 'rawId': raw.id, 'summary': sample_summary(data, plan)}
                row.row_count = batch.row_count = len(data['items'])
                row.status = batch.status = 'completed'
            row.finished_at = batch.finished_at = now
