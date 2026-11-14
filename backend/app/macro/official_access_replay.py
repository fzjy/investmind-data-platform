"""独立零HTTP恢复：引用诊断真实Raw，保留原403父作业及预算。"""
from uuid import UUID,uuid5

from sqlalchemy import select

from app.db.models import JobRun,IngestionBatch,SourcePayload
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import digest,canonical
from app.macro.extensions.store import instant
from app.macro.official_access_check import OfficialAccessCheckStore,record_value,validate_parent_id
from app.macro.official_macro_store import OfficialMacroStore,DATASET,normalize_document

JOB='macro-official-access-replay-v1:householdCredit'
NAMESPACE=UUID('8df15db1-3268-408d-82cd-aab9f8917dd9')


def replay_key(parent_id):
    validate_parent_id(parent_id)
    return str(uuid5(NAMESPACE,parent_id))


def _checked_probe(factory,session,parent_id,now):
    probe=OfficialAccessCheckStore(factory,parent_id);state=probe.evidence(session,now)
    if state['phase']!='completed' or state['result']['validation']!='passed':
        raise ValueError('OFFICIAL_REPLAY_SOURCE_NOT_READY')
    row=state['row'];batch=session.get(IngestionBatch,row.id);raw=session.get(SourcePayload,row.details['rawId'])
    parent=session.get(JobRun,parent_id)
    proof=dict(parentRunId=parent_id,accessCheckRunId=row.id,parentEvidenceHash=state['context']['parentEvidenceHash'],
        accessCheckEvidenceHash=digest(dict(run=record_value(row),batch=record_value(batch),rawHash=raw.content_hash)),
        parentFailedAt=utc(parent.finished_at).isoformat(),accessCheckCompletedAt=utc(row.finished_at).isoformat(),
        additionalHttpCount=1,replayHttpCount=0)
    body=probe._raw(session,row,state['context'],now)
    return state,proof,raw,body


def read_replay_raw(store,session,row):
    parent=row.details['recovery']['parentRunId']
    state,proof,raw,body=_checked_probe(store.factory,session,parent,utc(row.finished_at or row.started_at))
    if canonical(row.details['recovery'])!=canonical(proof) or type(row.details['rawId']) is not int or row.details['rawId']!=raw.id:
        raise ValueError('OFFICIAL_REPLAY_SOURCE_INVALID')
    return body,utc(raw.captured_at)


def validate_replay_run(store,session,row):
    d=row.details
    if (type(d) is not dict or set(d)!={'schemaVersion','plan','document','request','phase','rawId','normalized','coreBindings','recovery'}
            or type(d['schemaVersion']) is not int or d['schemaVersion']!=1 or type(d['recovery']) is not dict):
        raise ValueError('OFFICIAL_REPLAY_RUN_INVALID')
    now=utc(row.finished_at or row.started_at)
    source,proof,raw,body=_checked_probe(store.factory,session,d['recovery']['parentRunId'],now)
    context=source['context'];plan=context['plan'];doc=context['document'];phase=d['phase']
    store._root(session,plan)
    expected_business='access-replay:'+context['parentRunId']
    if (row.id!=replay_key(context['parentRunId']) or row.job_name!=JOB or row.business_key!=expected_business
            or row.request_count!=0 or phase not in ('received','completed') or canonical(d['recovery'])!=canonical(proof)
            or any(canonical(d[k])!=canonical(context[k]) for k in ('plan','document','request'))
            or type(d['rawId']) is not int or d['rawId']!=raw.id
            or utc(row.started_at)<utc(source['row'].finished_at) or type(d['coreBindings']) is not list):
        raise ValueError('OFFICIAL_REPLAY_RUN_INVALID')
    finished=phase=='completed';batch=session.get(IngestionBatch,row.id)
    if (row.status!=('completed' if finished else 'running') or row.error_message is not None
            or finished!=(row.finished_at is not None) or (finished and utc(row.finished_at)<utc(row.started_at))
            or batch is None or batch.source!='official' or batch.dataset!=DATASET+'householdCredit' or batch.mode!='raw-replay'
            or batch.request_count!=0 or batch.row_count!=row.row_count or batch.rejected_count!=0
            or utc(batch.started_at)!=utc(row.started_at) or batch.status!=('completed' if finished else 'processing')
            or finished!=(batch.finished_at is not None)
            or (finished and utc(batch.finished_at)!=utc(row.finished_at))):
        raise ValueError('OFFICIAL_REPLAY_BATCH_INVALID')
    if finished:
        if d['normalized']!=normalize_document(plan,doc,context['request'],body):
            raise ValueError('OFFICIAL_REPLAY_PROOF_INVALID')
        store._check_core(session,row)
    elif d['normalized'] is not None or d['coreBindings'] or row.row_count:
        raise ValueError('OFFICIAL_REPLAY_PROOF_INVALID')
    return d


def effective_replay(store,session,parent,as_of):
    row=session.get(JobRun,replay_key(parent.id))
    if row is None:return parent
    if row.details.get('phase')!='completed' or row.finished_at is None or utc(row.finished_at)>as_of:return parent
    validate_replay_run(store,session,row)
    return row


class OfficialAccessReplayStore:
    def __init__(self,factory,parent_id):
        validate_parent_id(parent_id)
        self.factory,self.parent_id=factory,parent_id
        self.key=replay_key(parent_id)

    def inspect(self,now):
        now=instant(now)
        with self.factory() as session:
            source,proof,raw,body=_checked_probe(self.factory,session,self.parent_id,now)
            row=session.get(JobRun,self.key);batch=session.get(IngestionBatch,self.key)
            if row is None:
                if batch is not None or session.scalar(select(JobRun).where(JobRun.job_name==JOB,JobRun.business_key=='access-replay:'+self.parent_id)) is not None:
                    raise ValueError('OFFICIAL_REPLAY_ORPHAN_INVALID')
                return dict(status='ready',runId=self.key,source=proof)
            validate_replay_run(OfficialMacroStore(self.factory),session,row)
            if utc(row.started_at)>now or (row.finished_at is not None and utc(row.finished_at)>now):
                raise ValueError('OFFICIAL_REPLAY_CLOCK_INVALID')
            return dict(status=row.details['phase'],runId=self.key,source=proof)

    def prepare(self,config,now):
        now=instant(now)
        state=self.inspect(now)
        if state['status']!='ready':return self.key
        with self.factory.begin() as session:
            session.get(JobRun,self.parent_id,with_for_update=True)
            probe=OfficialAccessCheckStore(self.factory,self.parent_id)
            live=probe._state(session,config,now)
            source,proof,raw,body=_checked_probe(self.factory,session,self.parent_id,now)
            if live['phase']!='completed' or proof!=state['source']:
                raise ValueError('OFFICIAL_REPLAY_SOURCE_CHANGED')
            existing=session.get(JobRun,self.key)
            if existing is not None:
                validate_replay_run(OfficialMacroStore(self.factory),session,existing)
                return self.key
            context=source['context']
            details=dict(schemaVersion=1,**{k:context[k] for k in ('plan','document','request')},
                phase='received',rawId=raw.id,normalized=None,coreBindings=[],recovery=proof)
            session.add(JobRun(id=self.key,job_name=JOB,business_key='access-replay:'+self.parent_id,status='running',
                started_at=now,request_count=0,row_count=0,details=details))
            session.add(IngestionBatch(id=self.key,source='official',dataset=DATASET+'householdCredit',mode='raw-replay',
                status='processing',started_at=now,request_count=0,row_count=0,rejected_count=0))
        return self.key
