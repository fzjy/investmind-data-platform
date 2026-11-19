from __future__ import annotations

from datetime import date, datetime, time, UTC
from decimal import Decimal

from sqlalchemy import select

from app.db.models import JobCursor
from app.ingestion.repositories import IngestionRepository, content_hash
from app.market.data.catalog import plan_market_targets
from app.market.data.repository import MarketQuoteRepository
from app.market.dataset_query import market_quote_statement
from app.market.pulse.repository import PulseRepository

from app.market.etf.facts import (
    JOB_NAME, FIELDS, fingerprint, identity, normalize, parameters, read_component, trading_components,
)

# 采集历史的恢复适配器仍引用这个入口。
_component = read_component


def quotes(session,catalog,day,now):
    targets={t.asset.asset_code:t for t in plan_market_targets(catalog,datetime.combine(day,time(23),UTC)) if t.asset.asset_type=='etfs'}
    result={}
    repo=MarketQuoteRepository(None)
    for row in session.scalars(market_quote_statement('etfs', day, day)):
        if row.asset_code in targets:
            try:
                if repo._lineage_valid(session,row,targets[row.asset_code],now):
                    result[row.asset_code]=row
            except (ValueError,TypeError,KeyError,ArithmeticError):
                pass
    return result


class EtfRepository:
    def __init__(self,factory):
        self.factory=factory

    def target(self,selection,now):
        day=PulseRepository(self.factory).target(selection,now)
        return day

    def component(self,code,day,api,fp,now):
        with self.factory() as session:
            return read_component(session,code,day,api,fp,now)

    def persist(self,code,day,api,fp,response,captured_at):
        if captured_at.utcoffset() is None:
            raise ValueError('ETF_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion=IngestionRepository(session)
            batch=ingestion.start_batch(f'market:etf:{api}','live')
            batch.started_at=batch.finished_at=captured_at
            batch.request_count=1
            raw=ingestion.save_payload(batch,api,parameters(code,day),FIELDS[api],response)
            raw.payload={**raw.payload,'etfRequest':parameters(code,day),'contractFingerprint':fp}
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
            from app.acquisition.adapters.markets import record_market
            record_market(session,api,code,parameters(code,day),captured_at,batch_id=batch.id)
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
        daily, share = trading_components(session, code, day, fp, now)
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
