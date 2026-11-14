"""显式失败父级的额外单次预算；不覆盖旧作业或root。"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.ops import JobRun
from app.db.models.raw import IngestionBatch
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import build_extension_plan, canonical
from app.macro.extensions.store import (
    ExtensionStore, JOB_NAME, RECOVERY_JOB_NAME, _job, instant, run_key,
    recovery_key, recovery_business_key,
)


class RecoveryStore(ExtensionStore):
    def __init__(self, factory, parent_id):
        super().__init__(factory)
        self.parent_id = parent_id

    def _failed_parent(self, session, now, *, lock=False):
        row = session.get(JobRun, self.parent_id, with_for_update=lock)
        if row is None or row.job_name != JOB_NAME:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        try:
            details = _job(row, row.details['plan']['groupId'])
        except (KeyError, TypeError):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID') from None
        if (details['phase'] != 'failed' or row.error_message != 'EXTENSION_RESPONSE_INVALID'
            or utc(row.finished_at) > instant(now)):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        self._root(session, details['plan'])
        return row

    def recovery_plan(self, config, now):
        with self.factory() as session:
            row = self._failed_parent(session, now)
            plan = row.details['plan']
            group = plan['groupId']
            expected = build_extension_plan(group, config, datetime.fromisoformat(plan['scheduledAt']))
            if not config.groups[group].collectionEnabled or canonical(plan) != canonical(expected):
                raise ExtensionError('EXTENSION_COLLECTION_CHANGED')
            return plan

    def attempt_key(self, plan):
        if run_key(plan) != self.parent_id:
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        return recovery_key(self.parent_id)

    def _inspect_recovery(self, session, plan, now):
        now = instant(now)
        parent = self._failed_parent(session, now)
        if canonical(parent.details['plan']) != canonical(plan):
            raise ExtensionError('EXTENSION_POLICY_CHANGED')
        row = session.get(JobRun, self.attempt_key(plan))
        if row is None:
            return {'status': 'new', 'runId': None, 'plan': plan, 'reservedCount': 0, 'errorCode': None}
        details = _job(row, plan['groupId'])
        self._parent(session, row)
        if utc(row.started_at) > now or (row.finished_at is not None and utc(row.finished_at) > now):
            raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
        if details['phase'] in ('received', 'completed'):
            self._raw(session, row)
        return {'status': 'uncertain' if details['phase'] == 'reserved' else details['phase'],
            'runId': row.id, 'plan': plan, 'reservedCount': 1, 'errorCode': row.error_message}

    def inspect(self, plan, now):
        with self.factory() as session:
            return self._inspect_recovery(session, plan, now)

    def reserve(self, plan, now):
        now = instant(now)
        try:
            with self.factory.begin() as session:
                self._failed_parent(session, now, lock=True)
                if self._inspect_recovery(session, plan, now)['status'] != 'new':
                    raise ExtensionError('EXTENSION_ALREADY_RESERVED')
                recent = session.scalars(select(JobRun).where(JobRun.job_name.in_((JOB_NAME, RECOVERY_JOB_NAME)),
                    JobRun.started_at > now - timedelta(seconds=60))).all()
                if sum(row.details.get('plan', {}).get('groupId') == plan['groupId'] for row in recent) >= plan['limits']['requestsPerMinute']:
                    raise ExtensionError('EXTENSION_RATE_LIMITED')
                key = self.attempt_key(plan)
                session.add(JobRun(id=key, job_name=RECOVERY_JOB_NAME,
                    business_key=recovery_business_key(plan, self.parent_id), status='running',
                    started_at=now, request_count=1, row_count=0,
                    details={'schemaVersion': 1, 'plan': plan, 'phase': 'reserved',
                        'rawId': None, 'coreBindings': [], 'recoveryOf': self.parent_id}))
                session.add(IngestionBatch(id=key, source=plan['source'], dataset=f"macro-extension:{plan['groupId']}",
                    mode='bounded-recovery', business_date=now, status='running', started_at=now,
                    request_count=1, row_count=0, rejected_count=0))
            return key
        except IntegrityError:
            raise ExtensionError('EXTENSION_RESERVATION_CONFLICT') from None
