"""复用严格单请求账本，仅保留原件；没有发行人解析器时不能生成证明。"""
from hashlib import sha256

from app.db.models.ops import JobRun
from app.macro.fund_contract_store import FundContractStore
from app.macro.issuer_contract_samples import validate_sample_plan,sample_run_key
from app.macro.official_member_budget import SAMPLE_JOB_PREFIX


class IssuerSampleStore(FundContractStore):
    def validate_plan(self,plan):return validate_sample_plan(plan)
    def job_name(self,plan):return SAMPLE_JOB_PREFIX+plan['document']['fundCode']
    def business_key(self,plan,document):return 'issuer-sample:'+document['fundCode']
    def run_key(self,plan,document):return sample_run_key(plan)
    def dataset(self,plan):return 'enterprise-issuer-samples'
    def api_name(self,plan,document):return 'official-issuer-contract-sample'

    def normalize(self,plan,document,body):raise ValueError('ISSUER_SAMPLE_NORMALIZER_NOT_IMPLEMENTED')

    def replay(self,key,now):raise ValueError('ISSUER_SAMPLE_NORMALIZER_NOT_IMPLEMENTED')

    def inspect(self,plan,now):
        state=super().inspect(plan,now);sample=None
        if state['phase']=='received':
            with self.factory() as session:
                row=session.get(JobRun,state['runId']);self._run(session,row)
                body,retrieved=self._raw(session,row)
                sample={'rawId':row.details['rawId'],'rawSha256':sha256(body).hexdigest(),
                    'byteLength':len(body),'retrievedAt':retrieved.isoformat(),
                    'declaredMediaType':state['plan']['document']['mediaType'],'identityVerified':False}
        return {**state,'sample':sample,'proofReady':False}
