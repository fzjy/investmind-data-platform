"""原住户403父级的独立单次诊断；不恢复根、不写Core或发布。"""
import asyncio
import base64
from datetime import datetime,timezone
from hashlib import sha256
from uuid import UUID,uuid5

from sqlalchemy import select

from app.db.models import IngestionBatch,JobRun,SourcePayload
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError,load_extension_config
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import build_extension_plan,digest
from app.macro.extensions.store import FAILURE_CODES,instant
from app.macro.official_macro_store import JOB as PARENT_JOB,OfficialMacroStore,normalize_document

JOB='official-macro-access-check-v1'
DATASET='official-macro-access-check:householdCredit'
NAMESPACE=UUID('e996cd69-5039-44c1-88f8-caf902a0268a')


def validate_parent_id(parent_id):
    if type(parent_id) is not str or str(UUID(parent_id))!=parent_id:
        raise ValueError('OFFICIAL_ACCESS_PARENT_INVALID')


def record_value(row):
    return {c.name:utc(value).isoformat() if isinstance(value,datetime) else value
        for c in row.__table__.columns for value in (getattr(row,c.name),)}


class OfficialAccessCheckStore:
    def __init__(self,factory,parent_id):
        validate_parent_id(parent_id)
        self.factory,self.parent_id=factory,parent_id
        self.key=str(uuid5(NAMESPACE,parent_id))

    def _parent_evidence(self,session,now,*,lock=False):
        row=session.get(JobRun,self.parent_id,with_for_update=lock)
        if row is None or row.job_name!=PARENT_JOB+'householdCredit':
            raise ValueError('OFFICIAL_ACCESS_PARENT_INVALID')
        d=OfficialMacroStore(self.factory)._run(session,row)
        if (d['phase']!='failed' or row.error_message!='EXTENSION_AUTH_ERROR'
                or d['document']['role']!='household-ytd' or d['document']['derivedFrom'] is not None
                or utc(row.finished_at)>now):
            raise ValueError('OFFICIAL_ACCESS_PARENT_INVALID')
        batch=session.get(IngestionBatch,row.id)
        return dict(parentRunId=row.id,parentEvidenceHash=digest(dict(run=record_value(row),batch=record_value(batch))),
            plan=d['plan'],document=d['document'],request=d['request'])

    def _parent(self,session,config,now,*,lock=False):
        context=self._parent_evidence(session,now,lock=lock)
        expected=build_extension_plan('householdCredit',config,datetime.fromisoformat(context['plan']['scheduledAt']))
        if not config.groups['householdCredit'].collectionEnabled or expected!=context['plan']:
            raise ValueError('OFFICIAL_ACCESS_CONFIGURATION_CHANGED')
        return context

    def _raw(self,session,row,context,now):
        raw=session.get(SourcePayload,row.details['rawId'])
        if (raw is None or raw.batch_id!=row.id or raw.api_name!='official-access-check/household-ytd'
                or raw.request_hash!=digest(context) or raw.content_hash!=digest(raw.payload)):
            raise ValueError('OFFICIAL_ACCESS_RAW_INVALID')
        v=raw.payload
        if (type(v) is not dict or set(v)!={'schemaVersion','context','bodyBase64','rawSha256','retrievedAt'}
                or type(v['schemaVersion']) is not int or v['schemaVersion']!=1 or v['context']!=context
                or type(v['bodyBase64']) is not str
                or len(v['bodyBase64'])>(context['plan']['limits']['maxResponseBytes']+2)//3*4):
            raise ValueError('OFFICIAL_ACCESS_RAW_INVALID')
        body=base64.b64decode(v['bodyBase64'],validate=True)
        retrieved=instant(datetime.fromisoformat(v['retrievedAt']))
        if (len(body)>context['plan']['limits']['maxResponseBytes'] or sha256(body).hexdigest()!=v['rawSha256']
                or retrieved.isoformat()!=v['retrievedAt'] or retrieved!=utc(raw.captured_at)
                or not utc(row.started_at)<=retrieved<=now):
            raise ValueError('OFFICIAL_ACCESS_RAW_INVALID')
        return body

    def _proof(self,context,body):
        try:
            normalized=normalize_document(context['plan'],context['document'],context['request'],body)
            return dict(validation='passed',pointCount=len(normalized['points']),
                periods=sorted({p['period'] for p in normalized['points']}),normalizerHash=digest(normalized))
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
            return dict(validation='failed',pointCount=0,periods=[],normalizerHash=None)

    def _state(self,session,config,now,*,frozen=False):
        context=self._parent_evidence(session,now) if frozen else self._parent(session,config,now)
        rows=list(session.scalars(select(JobRun).where(JobRun.job_name==JOB,JobRun.business_key=='parent:'+self.parent_id)))
        row=session.get(JobRun,self.key);batch=session.get(IngestionBatch,self.key)
        raws=list(session.scalars(select(SourcePayload).where(SourcePayload.batch_id==self.key)))
        if row is None:
            if rows or batch is not None or raws:raise ValueError('OFFICIAL_ACCESS_ORPHAN_INVALID')
            return dict(phase='new',context=context,row=None,result=None)
        if (len(rows)!=1 or rows[0].id!=self.key or row.job_name!=JOB or row.business_key!='parent:'+self.parent_id
                or batch is None or batch.source!='official' or batch.dataset!=DATASET or batch.mode!='access-check'
                or row.request_count!=1 or row.row_count!=0 or batch.request_count!=1 or batch.row_count!=0 or batch.rejected_count!=0
                or utc(row.started_at)!=utc(batch.started_at)
                or not utc(session.get(JobRun,self.parent_id).finished_at)<=utc(row.started_at)<=now):
            raise ValueError('OFFICIAL_ACCESS_RUN_INVALID')
        d=row.details
        if (type(d) is not dict or set(d)!={'schemaVersion',*context,'phase','rawId','result'}
                or type(d['schemaVersion']) is not int or d['schemaVersion']!=1
                or any(d[k]!=v for k,v in context.items()) or d['phase'] not in ('reserved','received','completed','failed')):
            raise ValueError('OFFICIAL_ACCESS_RUN_INVALID')
        phase=d['phase'];finished=phase in ('completed','failed')
        if (row.status!=('running' if not finished else phase) or finished!=(row.finished_at is not None)
                or finished!=(batch.finished_at is not None)
                or batch.status!={'reserved':'running','received':'processing','completed':'completed','failed':'failed'}[phase]
                or (finished and (utc(row.finished_at)!=utc(batch.finished_at) or not utc(row.started_at)<=utc(row.finished_at)<=now))
                or (phase=='failed' and row.error_message not in FAILURE_CODES)
                or (phase!='failed' and row.error_message is not None)):
            raise ValueError('OFFICIAL_ACCESS_RUN_INVALID')
        if phase in ('reserved','failed'):
            if raws or d['rawId'] is not None or d['result'] is not None:raise ValueError('OFFICIAL_ACCESS_RUN_INVALID')
        else:
            if type(d['rawId']) is not int or len(raws)!=1 or raws[0].id!=d['rawId']:
                raise ValueError('OFFICIAL_ACCESS_RAW_INVALID')
            body=self._raw(session,row,context,now)
            if (phase=='received' and d['result'] is not None) or (phase=='completed' and d['result']!=self._proof(context,body)):
                raise ValueError('OFFICIAL_ACCESS_PROOF_INVALID')
            if finished and utc(raws[0].captured_at)>utc(row.finished_at):raise ValueError('OFFICIAL_ACCESS_RUN_INVALID')
        return dict(phase=phase,context=context,row=row,result=d['result'])

    def inspect(self,config,now):
        with self.factory() as session:return self._state(session,config,instant(now))

    def evidence(self,session,now):
        """只读历史证明；新预留/发送路径始终使用当前策略。"""
        return self._state(session,None,instant(now),frozen=True)

    def reserve(self,config,now,*,expected_context=None):
        now=instant(now)
        with self.factory.begin() as session:
            self._parent(session,config,now,lock=True)
            state=self._state(session,config,now)
            if state['phase']!='new':raise ValueError('OFFICIAL_ACCESS_ALREADY_RESERVED')
            if expected_context is not None and state['context']!=expected_context:
                raise ValueError('OFFICIAL_ACCESS_CONTEXT_CHANGED')
            session.add(JobRun(id=self.key,job_name=JOB,business_key='parent:'+self.parent_id,status='running',
                started_at=now,request_count=1,row_count=0,details=dict(schemaVersion=1,**state['context'],phase='reserved',rawId=None,result=None)))
            session.add(IngestionBatch(id=self.key,source='official',dataset=DATASET,mode='access-check',status='running',
                started_at=now,request_count=1,row_count=0,rejected_count=0))
        return self.key

    def receive(self,config,body,now):
        now=instant(now)
        with self.factory.begin() as session:
            session.get(JobRun,self.key,with_for_update=True)
            state=self._state(session,config,now)
            if state['phase']!='reserved' or type(body) is not bytes or len(body)>state['context']['plan']['limits']['maxResponseBytes']:
                raise ValueError('OFFICIAL_ACCESS_RECEIVE_INVALID')
            payload=dict(schemaVersion=1,context=state['context'],bodyBase64=base64.b64encode(body).decode('ascii'),
                rawSha256=sha256(body).hexdigest(),retrievedAt=now.isoformat())
            raw=SourcePayload(batch_id=self.key,api_name='official-access-check/household-ytd',request_hash=digest(state['context']),
                content_hash=digest(payload),payload=payload,captured_at=now)
            session.add(raw);session.flush()
            state['row'].details={**state['row'].details,'phase':'received','rawId':raw.id}
            session.get(IngestionBatch,self.key).status='processing'

    def finish(self,config,now,*,error_code=None):
        now=instant(now)
        with self.factory.begin() as session:
            session.get(JobRun,self.key,with_for_update=True)
            state=self._state(session,config,now);row=state['row']
            if error_code is not None:
                if state['phase']!='reserved' or error_code not in FAILURE_CODES:raise ValueError('OFFICIAL_ACCESS_FINISH_INVALID')
                result=None;phase='failed'
            else:
                if state['phase']!='received':raise ValueError('OFFICIAL_ACCESS_FINISH_INVALID')
                result=self._proof(state['context'],self._raw(session,row,state['context'],now));phase='completed'
            row.details={**row.details,'phase':phase,'result':result};row.status=phase;row.finished_at=now;row.error_message=error_code
            batch=session.get(IngestionBatch,self.key);batch.status=phase;batch.finished_at=now


def execute_official_access_check(factory,parent_id,config,*,dry_run=False,transport=None,clock=None,config_loader=None,before_send=None):
    clock=clock or (lambda:datetime.now(timezone.utc));config_loader=config_loader or load_extension_config
    result=dict(readOnly=True,supplierHttp=0,requestCount=0,additionalHttpBudget=1,insertedRows=0,
        publicationRequested=False,recoveryCompleted=False,parentRunId=parent_id,
        supplierHttpCountBasis='transport-attempt-upper-bound')
    try:
        store=OfficialAccessCheckStore(factory,parent_id);state=store.inspect(config,clock())
        result.update(probeRunId=store.key,parentEvidenceHash=state['context']['parentEvidenceHash'],phase=state['phase'])
        if dry_run:return {**result,'status':'preview'}
        if state['phase']=='reserved':return {**result,'status':'blocked','errorCode':'OFFICIAL_ACCESS_ATTEMPT_UNCERTAIN'}
        if state['phase']=='failed':return {**result,'status':'blocked','errorCode':state['row'].error_message}
        if state['phase']=='new':
            expected_context=state['context']
            if before_send is not None:before_send()
            config=config_loader();state=store.inspect(config,clock())
            if state['context']!=expected_context:
                raise ValueError('OFFICIAL_ACCESS_CONTEXT_CHANGED')
            result['readOnly']=False
            try:store.reserve(config,clock(),expected_context=expected_context)
            except Exception:return {**result,'status':'blocked','phase':'unknown','requestCount':1,'errorCode':'OFFICIAL_ACCESS_RESERVATION_UNCERTAIN'}
            result['requestCount']=1
            result['phase']='reserved'
            # 事务提交后发送前再核策略；不退还已有槽位。
            config=config_loader();state=store.inspect(config,clock())
            result['supplierHttp']=1
            try:
                body=bytes(asyncio.run(ExtensionClient('public-official-document',transport=transport)._read(
                    state['context']['plan'],'GET',state['context']['request']['url'])))
            except Exception as exc:
                code=str(exc) if isinstance(exc,ExtensionError) and str(exc) in FAILURE_CODES else 'EXTENSION_EXECUTION_FAILED'
                try:
                    store.finish(config,clock(),error_code=code);result['phase']='failed'
                except Exception:
                    code='OFFICIAL_ACCESS_ATTEMPT_UNCERTAIN';result['phase']='unknown'
                return {**result,'status':'blocked','errorCode':code}
            try:store.receive(config,body,clock())
            except Exception:return {**result,'status':'blocked','phase':'unknown','errorCode':'OFFICIAL_ACCESS_RECEIVE_PENDING'}
            result['phase']='received'
        if state['phase']!='completed':
            result['readOnly']=False
            result['phase']='unknown'
            try:store.finish(config,clock())
            except Exception:return {**result,'status':'blocked','errorCode':'OFFICIAL_ACCESS_REPLAY_PENDING'}
        result['phase']='unknown'
        state=store.inspect(config,clock())
        return {**result,'phase':state['phase'],'status':'response_valid' if state['result']['validation']=='passed' else 'response_invalid',
            'validation':state['result']}
    except Exception:
        return {**result,'status':'blocked','errorCode':'OFFICIAL_ACCESS_CONTEXT_INVALID'}
