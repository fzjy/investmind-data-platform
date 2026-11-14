from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, UTC
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import IngestionBatch, JobCursor, SourcePayload, MarketDailyQuote
from app.ingestion.repositories import IngestionRepository, content_hash
from app.market.data.catalog import plan_market_targets
from app.market.data.repository import MarketQuoteRepository, aware
from app.market.pulse.repository import PulseRepository

JOB_NAME = 'sync-etf-heatmap'
FIELDS = {'fund_daily': ('ts_code','trade_date','close','pct_chg','vol','amount'),
          'fund_share': ('ts_code','trade_date','fd_share')}


def fingerprint(catalog):
    contract = {'version':1, 'assets':[a.model_dump(mode='json',by_alias=True) for a in catalog.assets if a.asset_type=='etfs'], 'fields':FIELDS}
    return hashlib.sha256(json.dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def identity(code, day, api, fp):
    return hashlib.sha256(f'etf-v1:{fp}:{code}:{day}:{api}'.encode()).hexdigest()


def parameters(code,day):
    return {'ts_code':code,'trade_date':day.strftime('%Y%m%d')}


def normalize(code,day,api,response):
    expected=FIELDS[api]
    if len(response.fields)!=len(expected) or set(response.fields)!=set(expected):
        raise ValueError('ETF_RESPONSE_REJECTED')
    if not response.items:
        raise ValueError('ETF_SOURCE_NOT_AVAILABLE')
    if len(response.items)!=1 or len(response.items[0])!=len(expected):
        raise ValueError('ETF_RESPONSE_REJECTED')
    row=dict(zip(response.fields,response.items[0],strict=True))
    if row['ts_code']!=code or row['trade_date']!=day.strftime('%Y%m%d'):
        raise ValueError('ETF_RESPONSE_REJECTED')
    result={}
    for key in expected[2:]:
        value=row[key]
        try:
            if isinstance(value,bool) or value is None:
                raise ValueError()
            number=Decimal(str(value))
            if not number.is_finite() or abs(number)>=Decimal('1e18'):
                raise ValueError()
            if key in {'close','fd_share'} and number<=0 or key in {'vol','amount'} and number<0:
                raise ValueError()
        except (ValueError,InvalidOperation):
            raise ValueError('ETF_RESPONSE_REJECTED') from None
        result[key]=number
    return result


def quotes(session,catalog,day,now):
    targets={t.asset.asset_code:t for t in plan_market_targets(catalog,datetime.combine(day,time(23),UTC)) if t.asset.asset_type=='etfs'}
    result={}
    repo=MarketQuoteRepository(None)
    for row in session.scalars(select(MarketDailyQuote).where(MarketDailyQuote.provider=='tushare',
        MarketDailyQuote.asset_type=='etfs',MarketDailyQuote.trade_date==day)):
        if row.asset_code in targets:
            try:
                if repo._lineage_valid(session,row,targets[row.asset_code],now):
                    result[row.asset_code]=row
            except (ValueError,TypeError,KeyError,ArithmeticError):
                pass
    return result


def _component(session,code,day,api,fp,now):
    cursor=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB_NAME,JobCursor.cursor_key==identity(code,day,api,fp)))
    try:
        proof=cursor.cursor_value
        raw_id=proof['sourcePayloadId']
        if type(raw_id) is not int or raw_id<=0:
            return None
        raw=session.get(SourcePayload,raw_id)
        if raw is None or raw.api_name!=api or raw.content_hash!=content_hash(raw.payload) or proof['contentHash']!=raw.content_hash:
            return None
        captured=aware(raw.captured_at)
        if captured>now or captured.astimezone(ZoneInfo('Asia/Shanghai')).date()<day:
            return None
        batch=session.get(IngestionBatch,raw.batch_id)
        if batch is None or batch.source!='tushare' or batch.dataset!=f'market:etf:{api}' or batch.status!='completed':
            return None
        if (batch.finished_at is None or aware(batch.started_at)!=captured or aware(batch.finished_at)!=captured
            or aware(cursor.updated_at)!=captured):
            return None
        params=parameters(code,day)
        if raw.payload['etfRequest']!=params or raw.request_hash!=request_hash(api,params,FIELDS[api]):
            return None
        values=normalize(code,day,api,TushareResponse.model_validate(raw.payload))
        return {**values,'sourcePayloadId':raw.id,'capturedAt':aware(raw.captured_at).isoformat()}
    except (KeyError,ValueError,TypeError,AttributeError):
        return None


class EtfRepository:
    def __init__(self,factory):
        self.factory=factory

    def target(self,selection,now):
        day=PulseRepository(self.factory).target(selection,now)
        return day

    def component(self,code,day,api,fp,now):
        with self.factory() as session:
            return _component(session,code,day,api,fp,now)

    def persist(self,code,day,api,fp,response,captured_at):
        if captured_at.utcoffset() is None:
            raise ValueError('ETF_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion=IngestionRepository(session)
            batch=ingestion.start_batch(f'market:etf:{api}','live')
            batch.started_at=batch.finished_at=captured_at
            batch.request_count=1
            raw=ingestion.save_payload(batch,api,parameters(code,day),FIELDS[api],response)
            raw.payload={**raw.payload,'etfRequest':parameters(code,day)}
            raw.content_hash=content_hash(raw.payload)
            raw.captured_at=captured_at
            try:
                normalize(code,day,api,response)
            except ValueError as exc:
                code=str(exc)
                batch.status,batch.rejected_count='rejected',len(response.items)
                ingestion.reject(batch,code)
                return code
            batch.status,batch.row_count='completed',1
            ingestion.upsert(JobCursor,{'job_name':JOB_NAME,'cursor_key':identity(code,day,api,fp)},
                {'cursor_value':{'sourcePayloadId':raw.id,'contentHash':raw.content_hash},'updated_at':captured_at})
            return None


def tiles(session,catalog,day,now):
    """只读重验全部来源；任一基金未就绪不发布半个九ETF分类。"""
    if session.scalar(select(JobCursor.id).where(JobCursor.job_name==JOB_NAME).limit(1)) is None:
        return []  # 尚未启用过本组的旧数据库不依赖0012报价表。
    source_quotes=quotes(session,catalog,day,now)
    fp=fingerprint(catalog)
    result=[]
    for asset in catalog.assets:
        if asset.asset_type!='etfs':
            continue
        code=asset.asset_code
        daily=_component(session,code,day,'fund_daily',fp,now)
        share=_component(session,code,day,'fund_share',fp,now)
        quote=source_quotes.get(code)
        if daily is None or share is None or quote is None:
            return []
        if daily['close']!=quote.close or daily['pct_chg']!=quote.pct_chg:
            return []
        result.append({'id':'etf:'+code,'name':asset.name,'assetKind':'etf',
            'change':float(daily['pct_chg']), 'marketCap':float(share['fd_share']*daily['close']/Decimal(10000)),
            'turnover':float(daily['amount']/Decimal(100000)), 'stockCount':None,'stocks':[],
            'etf':{'code':code,'close':float(daily['close']),'shares':float(share['fd_share']),
                'volume':float(daily['vol']),'tradeDate':day.isoformat(),'source':'tushare:fund_share+fund_daily',
                'capturedAt':{'quote':daily['capturedAt'],'shares':share['capturedAt']},
                'sourcePayloadIds':{'quote':daily['sourcePayloadId'],'shares':share['sourcePayloadId']}}})
    return result


def market_tiles(session,catalog,day,now,previous=None):
    """新日未收齐时，仅跨日携带仍经完整血缘复核的旧ETF及其真实旧日期。"""
    current=tiles(session,catalog,day,now)
    if current:
        return current
    if previous is None:
        from app.db.models import PublishedSnapshot
        base=session.get(PublishedSnapshot,'market-explorer')
        previous=base.payload if base else {}
    old=previous.get('heatmapData',{}).get('etf',[])
    if len(old)!=9:
        return []
    try:
        days={t['etf']['tradeDate'] for t in old}
        if len(days)!=1:
            return []
        prior=date.fromisoformat(next(iter(days)))
        if prior>=day:
            return []  # 同日损坏必须阻断，不能回退到已发布但失效的证据。
        verified=tiles(session,catalog,prior,now)
        return verified if verified==old else []
    except (KeyError,ValueError,TypeError):
        return []
