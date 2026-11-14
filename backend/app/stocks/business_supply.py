"""主营构成独立原件：范围二分恢复，不改旧字段注册表或Core。"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation
import json
from pathlib import Path
import re
from types import SimpleNamespace
from sqlalchemy import select,text
from app.connectors.tushare.contracts import request_hash
from app.db.models import IngestionBatch,JobCursor,PublishedSnapshot,SourcePayload
from app.ingestion.repositories import content_hash
from app.jobs.locks import job_lock_for_engine
from app.portfolio.api.dividends import distribution_date
from app.portfolio.api.dividends import original_dividend_rows
from app.portfolio.api.reference import capture_catalog

CONFIG_FILE=Path(__file__).resolve().parents[3]/'config'/'financial-indicators.json'
JOB='stock-business-evidence'
FIELDS=('ts_code','end_date','bz_item','bz_sales','bz_profit','bz_cost','curr_type','update_flag')
TYPES=('P','D')
ROW_CAP=100
MAX_RESPONSE_ROWS=8000  # 复用传输层安全上限；100为文档拆分阈值，实测可返回150。


def business_config():
    value=dict(enabled=False,maxHttpPerRun=20,timeoutSeconds=20,historyYears=10,refreshDays=7)
    if CONFIG_FILE.is_file():value.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get('businessSupply',{}))
    if type(value['enabled']) is not bool:raise ValueError('BUSINESS_CONFIG_INVALID')
    for key,maximum in [('maxHttpPerRun',20),('timeoutSeconds',120),('historyYears',30),('refreshDays',365)]:
        if type(value[key]) is not int or not 1<=value[key]<=maximum:raise ValueError('BUSINESS_CONFIG_INVALID')
    return value


def _utc(now):return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _start(end,years):
    try:return end.replace(year=end.year-years)
    except ValueError:return end.replace(year=end.year-years,day=28)


def _cursor(s,key):return s.scalar(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key==key))


def _scope(s):
    base=s.get(PublishedSnapshot,'screener')
    if base is None:raise ValueError('BUSINESS_BASE_REQUIRED')
    scope=sorted(r['symbol'] for r in base.payload['stocks'])
    if not scope or len(set(scope))!=len(scope) or any(not isinstance(v,str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',v) for v in scope):raise ValueError('BUSINESS_SCOPE_INVALID')
    return dict(scope=scope,sourceBaseVersion=base.version,asOf=_utc(base.as_of).date().isoformat())


def _contracts(s):
    result=[]
    for row in s.scalars(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id)):
        c=row.cursor_value
        if row.cursor_key!='contract:'+content_hash(c) or c.get('schemaVersion')!=1 or c.get('api')!='fina_mainbz' or c.get('fields')!=list(FIELDS) or c.get('types')!=list(TYPES) or c.get('rowCap')!=ROW_CAP:raise ValueError('BUSINESS_CONTRACT_INVALID')
        scope=c['scope']
        if not scope or scope!=sorted(set(scope)) or any(not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',v) for v in scope):raise ValueError('BUSINESS_CONTRACT_INVALID')
        if type(c['historyYears']) is not int or not 1<=c['historyYears']<=30 or type(c['refreshDays']) is not int or not 1<=c['refreshDays']<=365:raise ValueError('BUSINESS_CONTRACT_INVALID')
        if c['startDate']!=_start(date.fromisoformat(c['asOf']),c['historyYears']).isoformat() or c['endDate']!=c['asOf'] or not isinstance(c['sourceBaseVersion'],str) or not c['sourceBaseVersion']:raise ValueError('BUSINESS_CONTRACT_INVALID')
        _utc(datetime.fromisoformat(c['cycle']))
        result.append(c)
    return result


def _spec(symbol,kind,start,end):
    return dict(target=f'{symbol}:{kind}:{start}:{end}',api='fina_mainbz',fields=list(FIELDS),
        params=dict(ts_code=symbol,type=kind,start_date=start.replace('-',''),end_date=end.replace('-','')))


def _validate_rows(rows,spec):
    for row in rows:
        if row['ts_code']!=spec['params']['ts_code']:raise ValueError('BUSINESS_SYMBOL_INVALID')
        period=distribution_date(row['end_date'])
        if not period or not spec['params']['start_date']<=period.replace('-','')<=spec['params']['end_date'] or period[5:] not in {'03-31','06-30','09-30','12-31'}:raise ValueError('BUSINESS_PERIOD_INVALID')
        for field in ('bz_item','curr_type'):
            if row[field] is not None and not isinstance(row[field],str):raise ValueError('BUSINESS_TEXT_INVALID')
        for field in ('bz_sales','bz_profit','bz_cost'):
            if row[field] in (None,''):continue
            if isinstance(row[field],bool) or not Decimal(str(row[field])).is_finite():raise ValueError('BUSINESS_NUMBER_INVALID')
        if row['update_flag'] not in (None,'','0','1',0,1) or isinstance(row['update_flag'],bool):raise ValueError('BUSINESS_VERSION_INVALID')


def _validate(response,spec):
    if len(response.items)>MAX_RESPONSE_ROWS:raise ValueError('BUSINESS_ROW_CAP_INVALID')
    _validate_rows(response.rows(),spec)


def _observed_split_parent(s,c,spec,u):
    """只核准已保存的成功信封作拆分导航，永不作为经济叶数据。"""
    if u.get('status')!='failed' or u.get('errorType')!='ValueError':return None
    raw=s.get(SourcePayload,u.get('rawId'));batch=s.get(IngestionBatch,u.get('batchId'))
    digest=request_hash(spec['api'],spec['params'],spec['fields'])
    if raw is None or batch is None or raw.batch_id!=batch.id or raw.api_name!=spec['api'] or raw.request_hash!=digest or u.get('requestHash')!=digest or batch.dataset!=JOB or batch.source!='tushare' or batch.status!='failed' or batch.request_count!=1 or batch.rejected_count!=1 or batch.finished_at is None or raw.content_hash!=u.get('responseContentHash') or content_hash(raw.payload)!=raw.content_hash or set(raw.payload)!={'_http'}:return None
    try:
        data=json.loads(base64.b64decode(raw.payload['_http']['bodyBase64']))['data']
        # 只在内存补规范块，_original_rows仍从原字节Decimal解析并核验摘要。
        normalized=SimpleNamespace(id=raw.id,payload=raw.payload|dict(fields=data['fields'],items=data['items']))
        rows=original_dividend_rows(normalized,list(FIELDS),allow_more=True)
        if not ROW_CAP<len(rows)<=MAX_RESPONSE_ROWS or len(rows)!=u.get('rowCount') or len(rows)!=batch.row_count:return None
        _validate_rows(rows,spec)
    except (ValueError,KeyError,TypeError,OverflowError):return None
    receipt=dict(parserRevision=2,kind='observed-oversize-split-parent',consumable=False,
        contractIdentity=content_hash(c),unitKey=content_hash(c)+':'+spec['target'],unitHash=content_hash(u),
        rawId=raw.id,rawHash=raw.content_hash,requestHash=digest,rowCount=len(rows),
        splitThreshold=ROW_CAP,maxResponseRows=MAX_RESPONSE_ROWS)
    return (raw,rows,data.get('has_more')),receipt


def _acknowledge_split_parents(factory,policy):
    with factory.begin() as s:
        source=_scope(s)
        for c in _contracts(s):
            if c['scope']!=source['scope'] or any(c[k]!=policy[k] for k in ('historyYears','refreshDays')):continue
            specs,_,_,_=_state(s,c,preview=True)
            for spec in specs:
                unit=_cursor(s,content_hash(c)+':'+spec['target'])
                if unit is None or unit.cursor_value.get('status')!='failed':continue
                observed=_observed_split_parent(s,c,spec,unit.cursor_value)
                if observed is not None:
                    key='split-parent:'+content_hash(c)+':'+spec['target']
                    existing=_cursor(s,key)
                    if existing is None:s.add(JobCursor(job_name=JOB,cursor_key=key,cursor_value=observed[1]))
                    elif existing.cursor_value!=observed[1]:raise ValueError('BUSINESS_PARENT_RECEIPT_INVALID')


def _saved(s,c,spec,*,strict,preview=False):
    cursor=_cursor(s,content_hash(c)+':'+spec['target'])
    receipt=_cursor(s,'split-parent:'+content_hash(c)+':'+spec['target'])
    if cursor is None:
        if receipt is not None:raise ValueError('BUSINESS_PARENT_RECEIPT_INVALID')
        return None
    u=cursor.cursor_value
    if receipt is not None:
        observed=_observed_split_parent(s,c,spec,u)
        if observed is None or receipt.cursor_value!=observed[1]:raise ValueError('BUSINESS_PARENT_RECEIPT_INVALID')
        return observed[0]
    if u.get('status')!='completed':
        observed=_observed_split_parent(s,c,spec,u)
        if preview and observed is not None:return observed[0]
        if strict:raise ValueError('BUSINESS_UNRESOLVED_RESERVATION')
        return None
    raw=s.get(SourcePayload,u.get('rawId'));batch=s.get(IngestionBatch,u.get('batchId'))
    digest=request_hash(spec['api'],spec['params'],spec['fields'])
    if raw is None or batch is None or raw.batch_id!=batch.id or raw.api_name!=spec['api'] or raw.request_hash!=digest or u.get('requestHash')!=digest or batch.dataset!=JOB or batch.source!='tushare' or batch.status!='completed' or batch.request_count!=1 or batch.rejected_count!=0 or batch.finished_at is None or raw.content_hash!=u.get('responseContentHash') or content_hash(raw.payload)!=raw.content_hash:raise ValueError('BUSINESS_EVIDENCE_INVALID')
    rows=original_dividend_rows(raw,list(FIELDS),allow_more=True)
    if len(rows)!=u.get('rowCount') or len(rows)!=batch.row_count or len(rows)>MAX_RESPONSE_ROWS:raise ValueError('BUSINESS_ROW_COUNT_INVALID')
    _validate_rows(rows,spec)
    data=json.loads(base64.b64decode(raw.payload['_http']['bodyBase64']))['data']
    return raw,rows,data.get('has_more')


def _state(s,c,*,strict=False,preview=False):
    specs=[_spec(symbol,kind,c['startDate'],c['endDate']) for symbol in c['scope'] for kind in c['types']]
    saved=[];leaves=[];index=0
    while index<len(specs):
        spec=specs[index];index+=1
        item=_saved(s,c,spec,strict=strict,preview=preview)
        if item is None:continue
        raw,rows,more=item;saved.append((spec,raw,rows,more))
        if len(rows)>=ROW_CAP or more is True:
            if not rows:raise ValueError('BUSINESS_EMPTY_MORE')
            start=date.fromisoformat(distribution_date(spec['params']['start_date']));end=date.fromisoformat(distribution_date(spec['params']['end_date']))
            if start==end:raise ValueError('BUSINESS_ROW_CAP_EXCEEDED')
            middle=start+(end-start)//2
            specs.extend([_spec(spec['params']['ts_code'],spec['params']['type'],start.isoformat(),middle.isoformat()),
                _spec(spec['params']['ts_code'],spec['params']['type'],(middle+timedelta(days=1)).isoformat(),end.isoformat())])
            if len(specs)>10000:raise ValueError('BUSINESS_SPLIT_LIMIT')
        else:leaves.append((spec,raw,rows,more))
    return specs,saved,leaves,len(saved)==len(specs)


def capture(factory,client=None,*,execute=False,now=None,max_http=None):
    now=_utc(now or datetime.now(timezone.utc));spent=0
    lock=job_lock_for_engine(factory.kw['bind']);key=JOB+':targets';acquired=execute and lock.try_acquire(key)
    if execute and not acquired:return dict(status='locked',requestCount=0)
    try:
        policy=business_config()
        if not policy['enabled']:return dict(status='not_selected',requestCount=0)
        budget=policy['maxHttpPerRun'] if max_http is None else max_http
        if type(budget) is not int or not 1<=budget<=policy['maxHttpPerRun']:raise ValueError('BUSINESS_BUDGET_INVALID')
        if execute:_acknowledge_split_parents(factory,policy)
        with factory() as s:
            if factory.kw['bind'].dialect.name=='postgresql':s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            source=_scope(s)
            if source['asOf']>now.date().isoformat():raise ValueError('BUSINESS_FUTURE_BASE')
            pending=reusable=None
            for old in _contracts(s):
                _,saved,_,ready=_state(s,old,strict=True,preview=not execute)
                if not ready:
                    if pending is not None:raise ValueError('BUSINESS_MULTIPLE_PENDING')
                    pending=old
                marker=_cursor(s,'completed:'+content_hash(old))
                stamp=_utc(datetime.fromisoformat(marker.cursor_value['completedAt'])) if marker else max((_utc(r.captured_at) for _,r,_,_ in saved),default=now)
                if ready and old['scope']==source['scope'] and old['asOf']<=source['asOf'] and old['historyYears']==policy['historyYears'] and old['refreshDays']==policy['refreshDays'] and timedelta(0)<=now-stamp<timedelta(days=policy['refreshDays']):reusable=old
            c=pending or reusable or (source|dict(schemaVersion=1,api='fina_mainbz',fields=list(FIELDS),types=list(TYPES),rowCap=ROW_CAP,
                startDate=_start(date.fromisoformat(source['asOf']),policy['historyYears']).isoformat(),endDate=source['asOf'],
                historyYears=policy['historyYears'],refreshDays=policy['refreshDays'],cycle=now.isoformat()))
            if c['scope']!=source['scope']:raise ValueError('BUSINESS_FROZEN_SCOPE_CHANGED')
            if pending and any(c[k]!=policy[k] for k in ('historyYears','refreshDays')):raise ValueError('BUSINESS_FROZEN_POLICY_CHANGED')
            specs,saved,leaves,ready=_state(s,c,strict=True,preview=not execute)
        while execute and not ready and spent<budget:
            with factory() as s:missing=[spec for spec in specs if _saved(s,c,spec,strict=True) is None]
            report=capture_catalog(factory,client,execute=True,_job=JOB,_requests=missing,_contract=c,
                _control_key='contract:'+content_hash(c),_config=dict(maxHttpPerRun=budget-spent),_validator=_validate,_stop_on_error=True)
            spent+=report['requestCount']
            with factory() as s:specs,saved,leaves,ready=_state(s,c,strict=True)
            if report['failedCount'] or report['status']=='blocked' or report['requestCount']==0:raise ValueError('BUSINESS_CAPTURE_BLOCKED')
        if execute and ready:
            with factory.begin() as s:
                if _cursor(s,'completed:'+content_hash(c)) is None:s.add(JobCursor(job_name=JOB,cursor_key='completed:'+content_hash(c),cursor_value=dict(completedAt=now.isoformat())))
        return dict(status=('completed' if ready else 'pending') if execute else 'planned',requestCount=spent,identity=content_hash(c),
            unitCount=len(specs),completedCount=len(saved),leafCount=len(leaves),scope=c['scope'],asOf=c['asOf'],sourceBaseVersion=c['sourceBaseVersion'],
            compatibleTarget=c['scope']==source['scope'] and c['asOf']<=source['asOf'])
    except Exception as exc:
        return dict(status='blocked',requestCount=spent,code=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('BUSINESS_') else 'BUSINESS_EVIDENCE_INVALID')
    finally:
        if acquired:lock.release(key)


def load_business_evidence(factory,symbols,as_of):
    if factory is None:return {}
    with factory() as s:
        contracts=_contracts(s)
        if not contracts:return {}
        source=_scope(s)
        if not set(source['scope'])<=set(symbols):return {}
        selected=None
        for c in contracts:
            if c['scope']!=source['scope'] or c['asOf']>as_of.isoformat():continue
            try:_,_,leaves,ready=_state(s,c)
            except ValueError as exc:
                # 请求均保存成功不代表业务批次可用。单日仍截断等明确来源
                # 边界不应抹掉较早的完成资料；摘要/批次损坏仍必须抛错。
                if str(exc) not in {'BUSINESS_ROW_CAP_EXCEEDED','BUSINESS_EMPTY_MORE','BUSINESS_SPLIT_LIMIT'}:raise
                continue
            if ready and (selected is None or c['cycle']>selected[0]['cycle']):selected=c,leaves
        if selected is None:return {}
        c,leaves=selected
        result={code:dict(rows=[],queryComplete=True,sourceAsOf=c['asOf'],sourceBaseVersion=c['sourceBaseVersion'],capturedAt='',historyComplete=False,visibility='report-period-only') for code in c['scope']}
        for spec,raw,rows,more in leaves:
            item=result[spec['params']['ts_code']]
            item['queryComplete'] &= more is False
            item['capturedAt']=max(item['capturedAt'],_utc(raw.captured_at).isoformat())
            item['rows'].extend(r|dict(businessType=spec['params']['type']) for r in rows)
        return result


def run_business_step(settings,*,now=None,dry_run=True,publish_requested=False):
    from app.db.session import create_engine_and_session
    from app.shared.readonly_database import open_readonly_factory
    from app.connectors.tushare.client import TushareClient
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    policy=business_config()
    if not policy['enabled']:return dict(status='not_selected',requestCount=0)
    if settings.data_backend!='database' or not settings.database_url:return dict(status='blocked',code='BUSINESS_DATABASE_REQUIRED',requestCount=0)
    with ExitStack() as resources:
        if dry_run:factory=resources.enter_context(open_readonly_factory(settings.database_url))
        else:
            engine,factory=create_engine_and_session(settings.database_url);resources.callback(engine.dispose)
        report=capture(factory,now=now)
        if dry_run or report['status']!='planned':return report
        client=None
        if report['completedCount']<report['unitCount']:
            if settings.tushare_token is None:return dict(status='blocked',code='TUSHARE_TOKEN_REQUIRED',requestCount=0)
            rate=settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip' else settings.tushare_standard_requests_per_minute
            client=resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),max_retries=0,timeout_seconds=policy['timeoutSeconds'],rate_limiter=RequestRateLimiter(rate)))
        report=capture(factory,client,execute=True,now=now)
        report['publishReady']=report['status']=='completed' and report['compatibleTarget']
        if report['publishReady'] and publish_requested:
            from app.stocks.marts.stock_research_runtime import rebuild_stock_research
            report['publication']=rebuild_stock_research(factory,dry_run=False)
            if report['publication']['status'] not in ('completed','no_change'):report['status']='blocked'
        return report
