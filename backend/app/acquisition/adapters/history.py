"""按游标读取既有凭证，复用采集器的原件和范围校验。"""
from datetime import datetime
from app.acquisition.adapters.imports import import_keys
from functools import lru_cache
from sqlalchemy import select
from app.acquisition.adapters.markets import commodity_subject, MARKET_APIS
from app.acquisition.contracts import Attempt, Evidence, EvidenceRecord, StateKey, utc
from app.db.models import DailyBasic, ExternalFile, ExternalRecord, IngestionBatch, Security, SourcePayload
from app.ingestion.repositories import content_hash
from app.market.data.catalog import MarketDataCatalog, plan_market_targets
from app.market.data.repository import MarketQuoteRepository
from app.market.etf.repository import _component, fingerprint
from app.market.pulse.repository import _valid
from app.market.region.repository import _proof

MARKET_JOBS = ('sync-etf-heatmap','sync-market-pulse','sync-security-region','sync-market-data','sync-commodities')


def historical_record(key, checked, scope, refs, *, status='success',error=None):
    identity = content_hash([key.entry_id,key.item_id,key.subject_key,checked.isoformat(),scope,status])
    proof = Evidence(status,checked,checked if status=='success' else None,error,'原始采集凭证已核对',scope,refs)
    return EvidenceRecord(Attempt(key,identity,checked,scope,'initialization'),proof)


def market_record(session,row,now,catalog):
    checked = utc(row.updated_at)
    if checked is None or checked > now:
        return None
    receipt = row.cursor_value
    if row.job_name == 'sync-market-data':
        target = next((t for t in plan_market_targets(catalog,checked) if t.identity==row.cursor_key),None)
        if not target:
            return None
        repo = MarketQuoteRepository(None)
        quotes = repo._rows(session,target)
        if not quotes or receipt.get('rows')!=repo._proof(quotes) or not all(repo._lineage_valid(session,q,target,checked) for q in quotes):
            return None
        api,code,params = target.asset.api_name,target.asset.asset_code,target.params
        refs = ({'rawId':quotes[-1].source_payload_id},)
    else:
        raw = session.get(SourcePayload,receipt.get('sourcePayloadId')) if receipt.get('sourcePayloadId') else None
        if not raw or utc(raw.captured_at)!=checked or raw.content_hash!=content_hash(raw.payload) or raw.content_hash!=receipt.get('contentHash'):
            return None
        refs = ({'batchId':raw.batch_id},)
        if row.job_name == 'sync-etf-heatmap':
            params = raw.payload.get('etfRequest',{})
            api,code = raw.api_name,params['ts_code']
            day = datetime.strptime(params['trade_date'],'%Y%m%d').date()
            if not _component(session,code,day,api,fingerprint(catalog),checked):
                return None
        elif row.job_name == 'sync-market-pulse':
            params = raw.payload['pulseRequest']
            api,code = 'limit_status',params['ts_code']
            day = datetime.strptime(params['trade_date'],'%Y%m%d').date()
            basic = session.scalar(select(DailyBasic).filter_by(ts_code=code,trade_date=day))
            if not basic or not _valid(session,basic,checked):
                return None
        elif row.job_name == 'sync-security-region':
            params = raw.payload['regionRequest']
            api,code = 'region',params['ts_code']
            security = session.get(Security,code)
            if not security or not _proof(session,security,checked):
                return None
        else:
            from app.market.commodities.contracts import request
            from app.market.commodities.repository import CommodityRepository
            api,params = raw.api_name,raw.payload['commodityRequest']
            day = datetime.fromisoformat(raw.payload['commodityAsOf']).date()
            req = request(api,params,catalog,day)
            if req['identity']!=row.cursor_key or not CommodityRepository(None,catalog).read(req,checked,session):
                return None
            code = commodity_subject(params)
    entry,kind,item = MARKET_APIS[api]
    return historical_record(StateKey(entry,item,kind,code),checked,{'source':'tushare','api':api,'params':params},refs)


def import_records(session,row,now):
    checked = utc(row.imported_at)
    if row.status != 'imported' or not checked or checked > now or row.rejected_count or not row.valid_count:
        return ()
    items = session.execute(select(ExternalRecord.record_type,ExternalRecord.payload['source'].as_string(),
        ExternalRecord.payload['seriesId'].as_string(),ExternalRecord.payload['contentType'].as_string())
        .where(ExternalRecord.external_file_id==row.id).distinct())
    keys = {pair for kind,source,series,content in items if source
            for pair in import_keys('external-'+kind,source,series if kind=='macro' else content)}
    return tuple(historical_record(key,checked,{'source':source,'fileHash':row.file_hash},({'fileId':row.id},))
                 for key,source in sorted(keys))
