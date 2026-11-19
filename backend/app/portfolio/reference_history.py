"""批量指数日线与完整交易日历核对；不抓网页，不加入可投资股票池。"""
from datetime import date,timezone
from decimal import Decimal,InvalidOperation
from math import isfinite
from sqlalchemy import select,func,text
from app.db.models import PublishedSnapshot,SourcePayload,IngestionBatch,JobCursor,TradeCalendar,DailyPrice
from app.stocks.api.financial import years_before
from app.stocks.api.datasets import calendar_query, daily_rows_query
from app.ingestion.repositories import content_hash
from app.jobs.locks import job_lock_for_engine
from app.connectors.tushare.contracts import request_hash
from app.portfolio.dividend_contract import _date
from app.portfolio.dividend_ledger import _original_rows
from app.portfolio.reference_catalog import JOB as CATALOG_JOB,FIELDS as CATALOG_FIELDS,TARGETS,IDENTITY,capture_catalog,reference_config

JOB='portfolio-reference-history'
FIELDS=('ts_code','trade_date','close')

def normalize_index_history(rows,code,calendar):
    points={}
    for row in rows:
        day=_date(row.get('trade_date'))
        value=row.get('close')
        if row.get('ts_code')!=code or day is None or day in points or isinstance(value,bool):raise ValueError('REFERENCE_HISTORY_IDENTITY')
        try:number=Decimal(str(value));numeric=float(number)
        except (ValueError,TypeError,InvalidOperation,OverflowError):raise ValueError('REFERENCE_HISTORY_VALUE') from None
        if not number.is_finite() or number<=0 or not isfinite(numeric) or numeric<=0:raise ValueError('REFERENCE_HISTORY_VALUE')
        points[day]=numeric
    if not calendar or set(points)!=set(calendar):raise ValueError('REFERENCE_HISTORY_CALENDAR_GAP')
    return [dict(date=day,price=points[day]) for day in sorted(points)]

def _cursor(session,job,key):
    return session.scalar(select(JobCursor).where(JobCursor.job_name==job,JobCursor.cursor_key==key))

def _saved(session,unit,api,dataset):
    if unit is None or unit.cursor_value.get('status')!='completed':raise ValueError('REFERENCE_EVIDENCE_REQUIRED')
    value=unit.cursor_value
    raw=session.get(SourcePayload,value.get('rawId'));batch=session.get(IngestionBatch,value.get('batchId'))
    if raw is None or batch is None or raw.batch_id!=batch.id or batch.dataset!=dataset or batch.source!='tushare' or batch.status!='completed' or batch.request_count!=1 or batch.rejected_count!=0 or batch.row_count!=value.get('rowCount') or raw.api_name!=api or raw.request_hash!=value.get('requestHash') or raw.content_hash!=value.get('responseContentHash') or content_hash(raw.payload)!=raw.content_hash:raise ValueError('REFERENCE_EVIDENCE_INVALID')
    return raw

def history_plan(session,*,as_of=None,start=None):
    base=session.get(PublishedSnapshot,'screener')
    if base is None:raise ValueError('SCREENER_BASE_REQUIRED')
    base_day=(base.as_of.replace(tzinfo=timezone.utc) if base.as_of.tzinfo is None else base.as_of).astimezone(timezone.utc).date()
    as_of=as_of or base_day
    if as_of!=base_day:raise ValueError('REFERENCE_BASE_NOT_READY')
    symbols=[s['symbol'] for s in base.payload.get('stocks',[])]
    if not symbols:raise ValueError('REFERENCE_SCOPE_EMPTY')
    first=session.scalar(daily_rows_query(DailyPrice, years_before(as_of,3), as_of, symbols)
                         .with_only_columns(func.min(DailyPrice.trade_date)))
    if first is None:raise ValueError('REFERENCE_PRICE_WINDOW_REQUIRED')
    start=start or first.isoformat()
    calendars=session.scalars(calendar_query(date.fromisoformat(start),as_of,'SSE').order_by(TradeCalendar.cal_date)).all()
    if len(calendars)!=(as_of-date.fromisoformat(start)).days+1:raise ValueError('REFERENCE_FULL_CALENDAR_REQUIRED')
    calendar=tuple(r.cal_date.isoformat() for r in calendars if r.is_open)
    if not calendar:raise ValueError('REFERENCE_CALENDAR_EMPTY')
    targets=[]
    for bare in TARGETS:
        raw=_saved(session,_cursor(session,CATALOG_JOB,f'{IDENTITY}:{bare}'),'index_basic',CATALOG_JOB)
        if raw.request_hash!=request_hash('index_basic',{'market':'CSI','symbol':bare},CATALOG_FIELDS):raise ValueError('REFERENCE_CATALOG_REQUEST_MISMATCH')
        rows=_original_rows(raw,list(CATALOG_FIELDS))
        if len(rows)!=1:raise ValueError('REFERENCE_CATALOG_NOT_UNIQUE')
        row=rows[0];code=row.get('ts_code')
        if not isinstance(code,str) or code.split('.')[0]!=bare or row.get('market')!='CSI' or '全收益' not in str(row.get('fullname','')) or '中证' not in str(row.get('publisher','')):raise ValueError('REFERENCE_TOTAL_RETURN_IDENTITY')
        targets.append(dict(target=bare,code=code,name=row['fullname'],catalogRaw=raw.id,catalogHash=raw.content_hash))
    contract=dict(schemaVersion=1,asOf=as_of.isoformat(),start=start,baseVersion=base.version,calendarHash=content_hash(calendar),targets=targets,fields=list(FIELDS))
    requests=[dict(target=t['target'],api='index_daily',params={'ts_code':t['code'],'start_date':start.replace('-',''),'end_date':as_of.strftime('%Y%m%d')},fields=FIELDS) for t in targets]
    return contract,requests,calendar

def _history_evidence_contract(session, current, requests, calendar):
    """选股器同日重发后，复用请求、日历和原件仍完全相同的冻结指数合同。"""
    current_key = f'contract:{content_hash(current)}'
    if _cursor(session, JOB, current_key) is not None:
        return current
    frozen_contracts = session.scalars(select(JobCursor).where(
        JobCursor.job_name == JOB, JobCursor.cursor_key.like('contract:%')
    ).order_by(JobCursor.id.desc()))
    for row in frozen_contracts:
        frozen = row.cursor_value
        if not isinstance(frozen.get('baseVersion'), str) or (frozen | {'baseVersion': current['baseVersion']}) != current:
            continue
        identity = content_hash(frozen)
        if row.cursor_key != f'contract:{identity}':
            raise ValueError('REFERENCE_CONTRACT_INVALID')
        frozen_requests, frozen_calendar = _frozen_inputs(session, frozen)
        if frozen_requests != requests or frozen_calendar != calendar:
            raise ValueError('REFERENCE_FROZEN_INPUTS_CHANGED')
        # 只有完整原件能代替新版本的采集。部分完成时仍由原恢复流程接管。
        if any(_cursor(session, JOB, f"{identity}:{t['target']}") is None for t in frozen['targets']):
            return current
        _load_contract(session, frozen, calendar)
        return frozen
    return current

def _frozen_inputs(session,contract):
    if contract.get('schemaVersion')!=1 or contract.get('fields')!=list(FIELDS):raise ValueError('REFERENCE_CONTRACT_INVALID')
    start,end=date.fromisoformat(contract['start']),date.fromisoformat(contract['asOf'])
    if start>end or tuple(t['target'] for t in contract['targets'])!=TARGETS:raise ValueError('REFERENCE_CONTRACT_INVALID')
    rows=session.scalars(calendar_query(start,end,'SSE').order_by(TradeCalendar.cal_date)).all()
    calendar=tuple(r.cal_date.isoformat() for r in rows if r.is_open)
    if len(rows)!=(end-start).days+1 or not calendar or content_hash(calendar)!=contract['calendarHash']:raise ValueError('REFERENCE_FROZEN_CALENDAR_CHANGED')
    requests=[]
    for target in contract['targets']:
        raw=_saved(session,_cursor(session,CATALOG_JOB,f"{IDENTITY}:{target['target']}"),'index_basic',CATALOG_JOB)
        if raw.id!=target['catalogRaw'] or raw.content_hash!=target['catalogHash']:raise ValueError('REFERENCE_FROZEN_CATALOG_CHANGED')
        original=_original_rows(raw,list(CATALOG_FIELDS))
        if len(original)!=1 or original[0].get('ts_code')!=target['code'] or original[0].get('fullname')!=target['name']:raise ValueError('REFERENCE_FROZEN_CATALOG_CHANGED')
        requests.append(dict(target=target['target'],api='index_daily',params={'ts_code':target['code'],'start_date':start.strftime('%Y%m%d'),'end_date':end.strftime('%Y%m%d')},fields=FIELDS))
    return requests,calendar


def _load_contract(session,contract,calendar):
    identity=content_hash(contract);result={}
    for target in contract['targets']:
        raw=_saved(session,_cursor(session,JOB,f"{identity}:{target['target']}"),'index_daily',JOB)
        params={'ts_code':target['code'],'start_date':contract['start'].replace('-',''),'end_date':contract['asOf'].replace('-','')}
        if raw.request_hash!=request_hash('index_daily',params,FIELDS):raise ValueError('REFERENCE_HISTORY_REQUEST_MISMATCH')
        prices=normalize_index_history(_original_rows(raw,list(FIELDS)),target['code'],calendar)
        result[target['code']]=dict(id=target['code'],code=target['code'],name=target['name'],returnBasis='total-return',coverage={'start':contract['start'],'end':contract['asOf'],'complete':True},prices=prices,sourceIds=[f"raw.source_payload:{target['catalogRaw']}",f'raw.source_payload:{raw.id}'])
    return result


def capture_history(factory,client=None,*,execute=False):
    lock=job_lock_for_engine(factory.kw['bind'])
    key=JOB+':targets'
    acquired=execute and lock.try_acquire(key)
    if execute and not acquired:return dict(status='blocked',requestCount=0,code='REFERENCE_HISTORY_LOCK_BUSY')
    try:return _capture_history(factory,client,execute=execute)
    finally:
        if acquired:lock.release(key)


def _capture_history(factory,client=None,*,execute=False):
    with factory(autoflush=False) as s:
        s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        pending=None
        try:
            # 不以新基版/日期绕过失败或未知预留；优先完成最早冻结批次。
            for row in s.scalars(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id)):
                frozen=row.cursor_value;identity=content_hash(frozen)
                if row.cursor_key!=f'contract:{identity}':raise ValueError('REFERENCE_CONTRACT_INVALID')
                units=[_cursor(s,JOB,f"{identity}:{t['target']}") for t in frozen['targets']]
                if any(u is not None and u.cursor_value.get('status')!='completed' for u in units):
                    raise ValueError('REFERENCE_UNRESOLVED_PRIOR_TARGET')
                old_requests,calendar=_frozen_inputs(s,frozen)
                if all(u is not None for u in units):_load_contract(s,frozen,calendar)
                elif pending is None:pending=(frozen,old_requests,calendar)
            try:current,current_requests,current_calendar=history_plan(s)
            except ValueError:
                if pending is None:raise
                current=None
            if current is not None:
                current = _history_evidence_contract(s, current, current_requests, current_calendar)
            contract,requests,calendar=pending or (current,current_requests,current_calendar)
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            return dict(status='blocked',requestCount=0,code=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('REFERENCE_') else 'REFERENCE_EVIDENCE_INVALID')
    key=content_hash(contract)
    result=capture_catalog(factory,client,execute=execute,_requests=requests,_job=JOB,_contract=contract,_control_key=f'contract:{key}')
    result['responsesComplete']=result.pop('catalogReady',False)
    result['historyReady']=False
    if result['responsesComplete']:
        try:
            with factory() as session:_load_contract(session,contract,calendar)
            result['historyReady']=current is not None and content_hash(current)==key
            if execute and not result['historyReady']:result.update(status='pending',code='REFERENCE_PREVIOUS_TARGET_FINISHED')
        except ValueError as exc:result.update(status='failed',code=str(exc))
    return result|dict(asOf=contract['asOf'],start=contract['start'],identity=key)

def load_benchmarks(factory,as_of,start):
    with factory(autoflush=False) as s:
        s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        try:current,requests,calendar=history_plan(s,as_of=as_of,start=start)
        except ValueError:return {}
        contract = _history_evidence_contract(s, current, requests, calendar)
        if any(_cursor(s,JOB,f"{content_hash(contract)}:{t['target']}") is None for t in contract['targets']):return {}
        return _load_contract(s,contract,calendar)

def run_history(settings,factory,*,execute=False,rate_limiter=None):
    if not execute:return capture_history(factory)
    if settings.tushare_token is None:return dict(status='blocked',requestCount=0,code='TUSHARE_TOKEN_REQUIRED')
    from app.connectors.tushare.client import TushareClient
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    config=reference_config()
    rate=settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip' else settings.tushare_standard_requests_per_minute
    with TushareClient(settings.tushare_token.get_secret_value(),max_retries=0,timeout_seconds=config['timeoutSeconds'],rate_limiter=rate_limiter or RequestRateLimiter(rate)) as client:return capture_history(factory,client,execute=True)

def valid_benchmarks(model,as_of):
    try:
        date.fromisoformat(as_of)
        if not isinstance(model,dict):return False
        for key,row in model.items():
            coverage=row['coverage'];points=row['prices'];refs=row['sourceIds']
            if row['id']!=key or row['code']!=key or not row['name'] or row['returnBasis']!='total-return' or coverage['complete'] is not True:return False
            start,end=coverage['start'],coverage['end']
            if date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or not start<=end<=as_of or not isinstance(points,list) or not points:return False
            days=[p['date'] for p in points]
            if days!=sorted(set(days)) or days[0]!=start or days[-1]!=end:return False
            if any(date.fromisoformat(p['date']).isoformat()!=p['date'] or type(p['price']) not in (int,float) or not isfinite(p['price']) or p['price']<=0 for p in points):return False
            if not isinstance(refs,list) or not refs or any(not isinstance(ref,str) or not ref for ref in refs):return False
        return True
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):return False
