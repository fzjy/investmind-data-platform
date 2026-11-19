"""ETF 当日交易与份额分量各自保留独立的原始回应和捕获时间。"""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import IngestionBatch, JobCursor, SourcePayload
from app.ingestion.repositories import content_hash
from app.market.data.repository import aware
from app.market.stored_contracts import choose_contract, job_fingerprints, legacy_candidate_catalog, valid_fingerprint

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


def read_component(session,code,day,api,fp,now):
    cursor=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB_NAME,JobCursor.cursor_key==identity(code,day,api,fp)))
    try:
        proof=cursor.cursor_value
        raw_id=proof['sourcePayloadId']
        if type(raw_id) is not int or raw_id<=0:
            return None
        raw=session.get(SourcePayload,raw_id)
        if raw is None or raw.api_name!=api or raw.content_hash!=content_hash(raw.payload) or proof['contentHash']!=raw.content_hash:
            return None
        if raw.payload.get("contractFingerprint", fp) != fp:
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


def trading_components(session, code, day, fingerprint, checked_at):
    return (read_component(session, code, day, "fund_daily", fingerprint, checked_at),
            read_component(session, code, day, "fund_share", fingerprint, checked_at))


def stored_trading_components(session, code, day, checked_at, *, catalog=None):
    params = parameters(code, day)
    hashes = [request_hash(api, params, fields) for api, fields in FIELDS.items()]
    raw_fps = set(session.scalars(select(SourcePayload.payload["contractFingerprint"].as_string())
        .join(IngestionBatch).where(SourcePayload.api_name.in_(FIELDS), SourcePayload.request_hash.in_(hashes),
            IngestionBatch.dataset.in_([f"market:etf:{api}" for api in FIELDS]),
            IngestionBatch.status == "completed", SourcePayload.captured_at <= checked_at).distinct()))
    fps = {fp for fp in raw_fps if valid_fingerprint(fp)}
    if None in raw_fps:
        fps.update(job_fingerprints(session, JOB_NAME, day, checked_at))
        candidate_catalog = legacy_candidate_catalog(catalog)
        if candidate_catalog is not None:
            fps.add(fingerprint(candidate_catalog))
    candidates = []
    for fp in sorted(fps):
        components = trading_components(session, code, day, fp, checked_at)
        present = [(api, row) for api, row in zip(FIELDS, components, strict=True) if row is not None]
        if present:
            values = {api: tuple(row[field] for field in FIELDS[api][2:]) for api, row in present}
            candidates.append((max(row["capturedAt"] for _, row in present), values, components))
    return choose_contract(candidates) or (None, None)
