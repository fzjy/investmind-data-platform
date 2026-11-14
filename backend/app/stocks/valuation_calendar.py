"""估值窗口的独立交易日历原件；不补写旧Core或修改估值冻结目标。"""
from datetime import date
from sqlalchemy import select
from app.db.models import JobCursor,SourcePayload,IngestionBatch
from app.ingestion.repositories import content_hash
from app.connectors.tushare.contracts import request_hash
from app.portfolio.api.reference import capture_catalog
from app.portfolio.api.dividends import original_dividend_rows
JOB='stock-valuation-calendar-evidence'
FIELDS=['exchange','cal_date','is_open']

def calendar_days(rows,start,end):
    days={}
    for row in rows:
        day=date.fromisoformat(str(row['cal_date']));status=str(row['is_open'])
        if row['exchange']!='SSE' or not start<=day<=end or status not in ('0','1'):raise ValueError('VALUATION_CALENDAR_INVALID')
        days.setdefault(day,set()).add(status)
    if len(days)!=(end-start).days+1 or any(len(v)!=1 for v in days.values()):raise ValueError('VALUATION_CALENDAR_INCOMPLETE')
    return sorted(day for day,status in days.items() if status=={'1'})

def _cursor(s,key):return s.scalar(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key==key))

def _saved(s,c):
    identity=content_hash(c);unit=_cursor(s,identity+':SSE')
    if unit is None:return None
    u=unit.cursor_value
    raw=s.get(SourcePayload,u.get('rawId'));batch=s.get(IngestionBatch,u.get('batchId'))
    params=dict(exchange='SSE',start_date=c['start'].replace('-',''),end_date=c['end'].replace('-',''))
    if u.get('status')!='completed' or raw is None or batch is None or raw.batch_id!=batch.id or batch.dataset!=JOB or batch.source!='tushare' or batch.finished_at is None or u.get('requestHash')!=request_hash('trade_cal',params,FIELDS) or batch.status!='completed' or batch.request_count!=1 or batch.rejected_count!=0 or raw.api_name!='trade_cal' or raw.request_hash!=request_hash('trade_cal',params,FIELDS) or raw.content_hash!=u.get('responseContentHash') or content_hash(raw.payload)!=raw.content_hash:raise ValueError('VALUATION_CALENDAR_EVIDENCE_INVALID')
    rows=original_dividend_rows(raw,FIELDS)
    if len(rows)!=u.get('rowCount') or len(rows)!=batch.row_count:raise ValueError('VALUATION_CALENDAR_ROW_COUNT_INVALID')
    return calendar_days(rows,date.fromisoformat(c['start']),date.fromisoformat(c['end']))

def _contracts(s):
    result=[]
    for row in s.scalars(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id)):
        c=row.cursor_value
        if row.cursor_key!='contract:'+content_hash(c) or c.get('fields')!=FIELDS or c.get('api')!='trade_cal':raise ValueError('VALUATION_CALENDAR_CONTRACT_INVALID')
        result.append(c)
    return result

def capture_calendar(factory,client,asof,source_version,*,execute=False,max_http=1):
    start=asof.replace(year=asof.year-5,day=min(asof.day,28)) if asof.month==2 else asof.replace(year=asof.year-5)
    with factory() as s:
        frozen=None
        for old in _contracts(s):
            saved=_saved(s,old)
            if saved is None and old['end']!=asof.isoformat():raise ValueError('VALUATION_CALENDAR_PRIOR_INCOMPLETE')
            if old['end']==asof.isoformat():frozen=old
        c=frozen or dict(api='trade_cal',fields=FIELDS,start=start.isoformat(),end=asof.isoformat(),sourceBaseVersion=source_version)
        saved=_saved(s,c)
    if saved is not None:return dict(status='completed',requestCount=0,publishReady=True)
    if not execute:return dict(status='planned',requestCount=0,publishReady=False)
    if max_http<1:return dict(status='pending',requestCount=0,publishReady=False)
    def validate(response,spec):calendar_days(response.rows(),start,asof)
    report=capture_catalog(factory,client,execute=True,_job=JOB,_contract=c,_control_key='contract:'+content_hash(c),_requests=[dict(target='SSE',api='trade_cal',fields=FIELDS,params=dict(exchange='SSE',start_date=start.strftime('%Y%m%d'),end_date=asof.strftime('%Y%m%d')))],_config={'maxHttpPerRun':1},_validator=validate,_stop_on_error=True)
    try:
        with factory() as s:ready=_saved(s,c) is not None
    except ValueError:
        return dict(status='blocked',requestCount=report['requestCount'],publishReady=False,code='VALUATION_CALENDAR_EVIDENCE_INVALID')
    return dict(status='completed' if ready else 'blocked',requestCount=report['requestCount'],publishReady=ready)

def load_calendar(factory,asof):
    with factory() as s:
        selected=[c for c in _contracts(s) if c['end']==asof.isoformat()]
        if not selected:return None
        return _saved(s,selected[-1])
