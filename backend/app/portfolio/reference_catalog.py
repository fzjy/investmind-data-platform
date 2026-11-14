"""全收益指数目录的独立有界发现；原件持久保存，不直接启用基准。"""
from copy import deepcopy
from datetime import datetime,timezone
import json
from math import isfinite
from sqlalchemy import select
from app.db.models import JobCursor,SourcePayload
from app.ingestion.repositories import IngestionRepository,content_hash
from app.connectors.tushare.contracts import request_hash
from app.jobs.locks import job_lock_for_engine
from app.portfolio.dividend_capture import CONFIG_FILE

JOB='portfolio-reference-catalog'
FIELDS=('ts_code','name','fullname','market','publisher','base_date','list_date','desc','exp_date')
TARGETS=('H00300','H00922')
IDENTITY=content_hash(dict(api='index_basic',targets=TARGETS,fields=FIELDS,market='CSI'))

def reference_config():
    # 独立wheel没有工作区根配置；使用有限默认值，仍可在工作区主配置覆盖。
    value=json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get('referenceEvidence',{}) if CONFIG_FILE.is_file() else {}
    result={'maxHttpPerRun':2,'timeoutSeconds':20}|value
    limit,timeout=result['maxHttpPerRun'],result['timeoutSeconds']
    if type(limit) is not int or not 1<=limit<=len(TARGETS) or type(timeout) not in (int,float) or not isfinite(timeout) or not 0<timeout<=120:raise ValueError('目录预算/超时无效')
    return result

def capture_catalog(factory,client=None,*,execute=False,_requests=None,_job=JOB,_contract=None,_control_key='control',_config=None,_validator=None,_stop_on_error=False):
    config=reference_config() if _config is None else _config
    limit=config['maxHttpPerRun']
    if type(limit) is not int or not 1<=limit<=(len(TARGETS) if _config is None else 10000):raise ValueError('目录预算无效')
    requests=_requests if _requests is not None else [dict(target=symbol,api='index_basic',params={'market':'CSI','symbol':symbol},fields=FIELDS) for symbol in TARGETS]
    identity=content_hash(_contract) if _contract is not None else IDENTITY
    def cursor(session,key):
        return session.scalar(select(JobCursor).where(JobCursor.job_name==_job,JobCursor.cursor_key==key))
    lock=job_lock_for_engine(factory.kw['bind'])
    acquired=execute and lock.try_acquire(_job)
    if execute and not acquired:return dict(status='blocked',requestCount=0,code='CATALOG_LOCK_BUSY')
    result=dict(status='planned',requestCount=0,failedCount=0,units=[])
    try:
        if _contract is not None:
            with factory() as s:
                control=cursor(s,_control_key)
                if control is not None and control.cursor_value!=_contract:
                    return {**result,'status':'blocked','code':'REFERENCE_TARGET_CHANGED'}
            if execute and control is None:
                with factory.begin() as s:s.add(JobCursor(job_name=_job,cursor_key=_control_key,cursor_value=_contract))
        for spec in requests:
            symbol,api,params,fields=spec['target'],spec['api'],spec['params'],tuple(spec['fields'])
            key=f'{identity}:{symbol}'
            with factory() as s:
                existing=cursor(s,key)
                if existing is not None:
                    value=deepcopy(existing.cursor_value)
                    if value.get('status')=='completed':
                        raw=s.get(SourcePayload,value.get('rawId'))
                        if raw is None or raw.api_name!=api or raw.batch_id!=value.get('batchId') or raw.request_hash!=request_hash(api,params,fields) or content_hash(raw.payload)!=value.get('responseContentHash') or raw.content_hash!=value.get('responseContentHash'):
                            value['status']='invalid_evidence'
                    result['units'].append(value);continue
            if not execute:
                result['units'].append(dict(symbol=symbol,status='pending'));continue
            if result['requestCount']>=limit:break
            with factory.begin() as s:
                batch=IngestionRepository(s).start_batch(_job,None)
                batch_id=batch.id;batch.request_count=1
                value=dict(symbol=symbol,status='reserved',batchId=batch_id,requestHash=request_hash(api,params,fields))
                s.add(JobCursor(job_name=_job,cursor_key=key,cursor_value=value))
            def observe(http):
                with factory.begin() as s:
                    saved=cursor(s,key)
                    if saved.cursor_value.get('rawId') is not None:raise ValueError('重复响应')
                    raw=SourcePayload(batch_id=batch_id,api_name=api,request_hash=value['requestHash'],payload={'_http':http},content_hash=content_hash({'_http':http}))
                    s.add(raw);s.flush()
                    saved.cursor_value=saved.cursor_value|{'rawId':raw.id}
            client.set_response_observer(observe)
            result['requestCount']+=1
            response=None;error=None
            try:
                response=client.call(api,params,fields)
                if tuple(response.fields)!=fields or len(response.items)>8000:raise ValueError('目录字段/行数无效')
                if _validator is not None:_validator(response,spec)
                elif any(str(r.get('ts_code','')).split('.')[0]!=symbol for r in response.rows()):raise ValueError('目录返回其他指数')
            except Exception as exc:error=type(exc).__name__
            from app.db.models import IngestionBatch
            with factory.begin() as s:
                saved=cursor(s,key);batch=s.get(IngestionBatch,batch_id)
                raw=s.get(SourcePayload,saved.cursor_value['rawId']) if saved.cursor_value.get('rawId') else None
                if response is not None and not error:
                    if raw is None or raw.payload['_http'].get('credentialsRedacted') is not False:
                        error='HTTP_ORIGINAL_REQUIRED'
                    else:
                        decoded={'fields':list(response.fields),'items':[list(r) for r in response.items]}
                        json.dumps(decoded,allow_nan=False)
                        raw.payload=raw.payload|decoded;raw.content_hash=content_hash(raw.payload)
                value=saved.cursor_value|dict(status='failed' if error else 'completed',errorType=error,rowCount=len(response.items) if response else 0,responseContentHash=raw.content_hash if raw is not None else None)
                saved.cursor_value=value;batch.status=value['status'];batch.row_count=value['rowCount'];batch.rejected_count=int(bool(error));batch.finished_at=datetime.now(timezone.utc)
                result['units'].append(value)
                result['failedCount']+=int(bool(error))
            if error and _stop_on_error:break
        result['failedCount']=sum(u['status'] in ('failed','reserved','invalid_evidence') for u in result['units'])
        result['catalogReady']=len(result['units'])==len(requests) and all(u['status']=='completed' and u.get('rowCount',0)>0 for u in result['units'])
        if execute:result['status']='failed' if result['failedCount'] else 'pending' if len(result['units'])<len(requests) else 'completed'
        return result
    finally:
        if acquired:lock.release(_job)

def run_catalog(settings,factory,*,execute=False,rate_limiter=None):
    if not execute:return capture_catalog(factory)
    if settings.tushare_token is None:return dict(status='blocked',code='TUSHARE_TOKEN_REQUIRED',requestCount=0)
    from app.connectors.tushare.client import TushareClient
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    config=reference_config()
    rate=settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip' else settings.tushare_standard_requests_per_minute
    with TushareClient(settings.tushare_token.get_secret_value(),max_retries=0,timeout_seconds=config['timeoutSeconds'],rate_limiter=rate_limiter or RequestRateLimiter(rate)) as client:
        return capture_catalog(factory,client,execute=True)
