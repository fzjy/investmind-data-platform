"""独立恢复原件；父失败不可变，新原件仍不是成员证明。"""
from datetime import datetime

from app.db.models.ops import JobRun
from app.macro.extensions.store import instant
from app.macro.issuer_sample_store import IssuerSampleStore
from app.macro.issuer_sample_recovery import validate_recovery_plan,recovery_run_key,failure_receipt
from app.macro.official_member_budget import RECOVERY_JOB_PREFIX


class IssuerRecoveryStore(IssuerSampleStore):
    def validate_plan(self,plan):return validate_recovery_plan(plan)
    def job_name(self,plan):return RECOVERY_JOB_PREFIX+plan['document']['fundCode']
    def business_key(self,plan,document):return 'issuer-recovery:'+document['fundCode']
    def run_key(self,plan,document):return recovery_run_key(plan)
    def dataset(self,plan):return 'enterprise-issuer-recovery'
    def api_name(self,plan,document):return 'official-issuer-sample-recovery'

    def _parent(self,session,plan):
        self.validate_plan(plan)
        row=session.get(JobRun,plan['parent']['runId'])
        if failure_receipt(session,row)!=plan['parent']:raise ValueError('ISSUER_RECOVERY_PARENT_CHANGED')

    def _root(self,session,plan):
        self._parent(session,plan)
        return super()._root(session,plan)

    def inspect(self,plan,now):
        with self.factory() as session:self._parent(session,plan)
        if instant(now)<datetime.fromisoformat(plan['parent']['finishedAt']):raise ValueError('ISSUER_RECOVERY_CLOCK_REGRESSION')
        return super().inspect(plan,now)
