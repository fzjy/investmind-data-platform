"""已知历史网络失败的单次具名恢复；原父不可变，所有请求共用原总额。"""
import asyncio
from datetime import datetime,timezone
from uuid import UUID,uuid5
from sqlalchemy import select,func

from app.db.models.ops import JobRun,JobCursor
from app.db.models.raw import IngestionBatch
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import digest
from app.macro.extensions.store import FAILURE_CODES
from app.macro.housing_history_plan import DIRECTORY_VERSION,REPORT_VERSION,VERSIONS
from app.macro.official_macro_store import OfficialMacroStore,job_name,dataset_name,run_key,normalize_document
from app.macro.official_access_check import record_value

JOB='tier1-housing-history-recovery-v1'
NAMESPACE=UUID('bde5df55-d7ca-5f2e-9bd5-c9281f98d226')
ELIGIBLE={'EXTENSION_TIMEOUT','EXTENSION_NETWORK_ERROR'}


def recovery_key(parent_id):
    if type(parent_id) is not str or str(UUID(parent_id))!=parent_id:raise ValueError('HISTORY_RECOVERY_PARENT_INVALID')
    return str(uuid5(NAMESPACE,parent_id))


def reserved_in_session(session):
    names=[DIRECTORY_VERSION+':tier1Housing',REPORT_VERSION+':tier1Housing',JOB]
    return session.scalar(select(func.coalesce(func.sum(JobRun.request_count),0)).where(JobRun.job_name.in_(names)))


def reserved_total(factory):
    with factory() as s:return reserved_in_session(s)


def parent_context(store,session,parent_id):
    recovery_key(parent_id);parent=session.get(JobRun,parent_id)
    if parent is None or parent.job_name not in (DIRECTORY_VERSION+':tier1Housing',REPORT_VERSION+':tier1Housing'):raise ValueError('HISTORY_RECOVERY_PARENT_INVALID')
    d=store._run(session,parent)
    if (d['plan']['normalizerVersion'] not in VERSIONS or d['phase']!='failed' or parent.error_message not in ELIGIBLE
        or d['rawId'] is not None or d['normalized'] is not None or d['coreBindings'] or parent.row_count!=0):raise ValueError('HISTORY_RECOVERY_NOT_ELIGIBLE')
    batch=session.get(IngestionBatch,parent_id)
    return parent,d,digest(dict(run=record_value(parent),batch=record_value(batch)))


def public_recovery(parent,batch,started):
    evidence=dict(run=record_value(parent),batch=record_value(batch))
    return dict(parentRunId=parent.id,parentEvidenceHash=digest(evidence),parentEvidence=evidence,parentErrorCode=parent.error_message,
        parentFailedAt=utc(parent.finished_at).isoformat(),recoveryStartedAt=utc(started).isoformat(),additionalHttpCount=1)


def validate_public_recovery(proof,parent_id,child_id,retrieved,completed,plan,doc):
    if (type(proof) is not dict or set(proof)!={'parentRunId','parentEvidenceHash','parentEvidence','parentErrorCode','parentFailedAt','recoveryStartedAt','additionalHttpCount'}
        or proof['parentRunId']!=parent_id or child_id!=recovery_key(parent_id) or proof['parentErrorCode'] not in ELIGIBLE
        or type(proof['additionalHttpCount']) is not int or proof['additionalHttpCount']!=1):raise ValueError('HISTORY_RECOVERY_PROOF_INVALID')
    import re
    if type(proof['parentEvidenceHash']) is not str or re.fullmatch('[0-9a-f]{64}',proof['parentEvidenceHash']) is None:raise ValueError()
    evidence=proof['parentEvidence'];run=evidence['run'];started_at=run['started_at'];failed_at=proof['parentFailedAt']
    expected=dict(run=dict(id=parent_id,job_name=job_name(plan),business_key=plan['groupId']+':'+plan['scheduledAt']+':'+doc['id'],
        status='failed',started_at=started_at,finished_at=failed_at,request_count=1,row_count=0,error_message=proof['parentErrorCode'],
        details=dict(schemaVersion=1,plan=plan,document=doc,request=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None),
            phase='failed',rawId=None,normalized=None,coreBindings=[])),
        batch=dict(id=parent_id,source='official',dataset=dataset_name(plan),mode='live',business_date=None,status='failed',started_at=started_at,
            finished_at=failed_at,request_count=1,row_count=0,rejected_count=0))
    if digest(evidence)!=proof['parentEvidenceHash'] or evidence!=expected:raise ValueError('HISTORY_RECOVERY_PARENT_EVIDENCE_INVALID')
    failed=datetime.fromisoformat(proof['parentFailedAt']);started=datetime.fromisoformat(proof['recoveryStartedAt'])
    if (failed.tzinfo!=timezone.utc or started.tzinfo!=timezone.utc
        or failed.isoformat()!=proof['parentFailedAt'] or started.isoformat()!=proof['recoveryStartedAt']
        or not datetime.fromisoformat(plan['scheduledAt'])<=datetime.fromisoformat(started_at)<=failed<=started<=datetime.fromisoformat(retrieved)<=datetime.fromisoformat(completed)):raise ValueError('HISTORY_RECOVERY_TIME_INVALID')


def validate_recovery_run(store,session,row):
    d=row.details
    if (type(d) is not dict or set(d)!={'schemaVersion','plan','document','request','phase','rawId','normalized','coreBindings','recovery'}
        or type(d['schemaVersion']) is not int or d['schemaVersion']!=1 or type(d['recovery']) is not dict):raise ValueError('HISTORY_RECOVERY_RUN_INVALID')
    parent,p,hash_value=parent_context(store,session,d['recovery']['parentRunId'])
    if (row.id!=recovery_key(parent.id) or row.job_name!=JOB or row.business_key!='recover:'+parent.id or row.request_count!=1
        or any(d[k]!=p[k] for k in ('plan','document','request')) or d['recovery']!=public_recovery(parent,session.get(IngestionBatch,parent.id),row.started_at)
        or utc(row.started_at)<utc(parent.finished_at) or type(d['coreBindings']) is not list):raise ValueError('HISTORY_RECOVERY_PARENT_CHANGED')
    phase=d['phase'];finished=phase in ('completed','failed');batch=session.get(IngestionBatch,row.id)
    if (phase not in ('reserved','received','completed','failed') or row.status!=('running' if not finished else phase)
        or finished!=(row.finished_at is not None) or (finished and utc(row.finished_at)<utc(row.started_at))
        or (row.error_message not in FAILURE_CODES if phase=='failed' else row.error_message is not None)
        or (phase!='completed' and (d['normalized'] is not None or d['coreBindings'] or row.row_count))
        or (phase in ('reserved','failed') and d['rawId'] is not None) or batch is None
        or batch.source!='official' or batch.dataset!=dataset_name(d['plan']) or batch.mode!='live' or batch.request_count!=1
        or batch.row_count!=row.row_count or batch.rejected_count!=0 or utc(batch.started_at)!=utc(row.started_at)
        or batch.status!={'reserved':'running','received':'processing','completed':'completed','failed':'failed'}[phase]
        or (batch.finished_at is None)!=(row.finished_at is None)
        or (batch.finished_at is not None and utc(batch.finished_at)!=utc(row.finished_at))):raise ValueError('HISTORY_RECOVERY_RUN_INVALID')
    if phase in ('received','completed'):
        body,retrieved=store._raw(session,row)
        if finished:
            if utc(row.finished_at)<retrieved or d['normalized']!=normalize_document(d['plan'],d['document'],d['request'],body):raise ValueError('HISTORY_RECOVERY_RAW_INVALID')
            store._check_core(session,row)
    return d


def effective_recovery(store,session,parent,as_of):
    child=session.get(JobRun,recovery_key(parent.id))
    if child is None or utc(child.started_at)>as_of:return parent
    validate_recovery_run(store,session,child)
    if child.details['phase']!='completed' or utc(child.finished_at)>as_of:return parent
    return child


def prepare(store,parent_id,now):
    key=recovery_key(parent_id)
    with store.factory.begin() as s:
        parent,p,hash_value=parent_context(store,s,parent_id)
        base=p['plan'].get('directoryPlan',p['plan'])
        s.scalar(select(JobCursor).where(JobCursor.job_name==job_name(base),JobCursor.cursor_key==base['scheduledAt']).with_for_update())
        parent=s.get(JobRun,parent_id,with_for_update=True);parent,p,hash_value=parent_context(store,s,parent_id)
        child=s.get(JobRun,key)
        if child is not None:
            validate_recovery_run(store,s,child)
            raise ValueError('HISTORY_RECOVERY_ALREADY_RESERVED')
        if now<utc(parent.finished_at) or reserved_in_session(s)>=base['groupPolicy']['maxHttpTotal']:raise ValueError('HISTORY_RECOVERY_BUDGET_OR_TIME_INVALID')
        details={**p,'phase':'reserved','recovery':public_recovery(parent,s.get(IngestionBatch,parent_id),now)}
        s.add(JobRun(id=key,job_name=JOB,business_key='recover:'+parent_id,status='running',started_at=now,request_count=1,row_count=0,details=details))
        s.add(IngestionBatch(id=key,source='official',dataset=dataset_name(p['plan']),mode='live',status='running',started_at=now,request_count=1,row_count=0,rejected_count=0))
    return key


def execute_recovery(factory,config,parent_id,*,clock=None,transport=None,dry_run=False,rate_limiter=None):
    from app.macro.jobs import housing_history_runtime as runtime
    from app.macro.extensions.client import ExtensionClient
    clock=clock or (lambda:datetime.now(timezone.utc));store=OfficialMacroStore(factory)
    out=dict(readOnly=True,supplierHttp=0,supplierHttpCountBasis='transport-attempt-upper-bound',publicationRequested=False)
    try:
        key=recovery_key(parent_id)
        with factory() as s:
            parent,p,_=parent_context(store,s,parent_id);child=s.get(JobRun,key)
            if child is not None:validate_recovery_run(store,s,child)
            phase=child.details['phase'] if child is not None else 'new'
        if dry_run:return {**out,'status':'preview','phase':phase,'parentRunId':parent_id,'runId':key,'reservedTotal':reserved_total(factory),'maxHttpTotal':p['plan']['groupPolicy']['maxHttpTotal']}
        if phase=='completed':return {**out,'status':'completed','runId':key}
        if phase in ('reserved','failed'):return {**out,'status':'blocked','errorCode':'HISTORY_RECOVERY_ALREADY_ATTEMPTED','runId':key}
        if phase=='new':
            if rate_limiter is not None:rate_limiter.acquire()
            current=runtime.load_extension_config().historyBackfills.get('tier1Housing')
            if current is None or not current.collectionEnabled or current.model_dump(exclude={'collectionEnabled'})!=p['plan']['groupPolicy']:
                return {**out,'status':'blocked','errorCode':'HISTORY_COLLECTION_CHANGED'}
            out['readOnly']=False
            try:prepare(store,parent_id,clock())
            except Exception:return {**out,'status':'blocked','errorCode':'HISTORY_RECOVERY_RESERVATION_BLOCKED','runId':key}
            out['supplierHttp']=1
            try:body=bytes(asyncio.run(ExtensionClient('public-official-document',transport=transport)._read(p['plan'],'GET',p['request']['url'])))
            except Exception as error:
                code=str(error) if str(error) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED';store.fail(key,code,clock())
                return {**out,'status':'blocked','errorCode':code,'runId':key}
            store.receive(key,body,clock())
        out['readOnly']=False
        try:store.replay(key,clock())
        except Exception:return {**out,'status':'replay_pending','errorCode':'HISTORY_RECOVERY_NORMALIZATION_PENDING','runId':key}
        return {**out,'status':'completed','runId':key}
    except Exception:return {**out,'status':'blocked','errorCode':'HISTORY_RECOVERY_INVALID'}
