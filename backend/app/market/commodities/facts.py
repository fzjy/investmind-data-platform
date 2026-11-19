"""从已入库的期货原件核验单品种的交易日、主力合约及逐日行情。"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import SourcePayload, IngestionBatch, JobCursor
from app.ingestion.repositories import content_hash
from app.market.data.repository import aware
from app.market.commodities.contracts import JOB_NAME, FIELDS, CommodityOptions, fingerprint, frozen_request, monthly_code
from app.market.stored_contracts import choose_contract, job_fingerprints, legacy_candidate_catalog, valid_fingerprint

CONTRACT_ASSETS = CommodityOptions().assets


def number(value,*,positive=False):
    if value is None or isinstance(value,bool): raise ValueError('COMMODITY_RESPONSE_REJECTED')
    try: result=Decimal(str(value))
    except InvalidOperation: raise ValueError('COMMODITY_RESPONSE_REJECTED') from None
    if not result.is_finite() or result<0 or result>=Decimal('1e18') or positive and result==0:
        raise ValueError('COMMODITY_RESPONSE_REJECTED')
    return result


def parse_day(value):
    if not isinstance(value,str) or len(value)!=8: raise ValueError('COMMODITY_RESPONSE_REJECTED')
    return datetime.strptime(value,'%Y%m%d').date()


def normalize(req,response,catalog):
    api,params=req['api'],req['params']
    fields=FIELDS[api]
    if len(response.fields)!=len(fields) or set(response.fields)!=set(fields): raise ValueError('COMMODITY_RESPONSE_REJECTED')
    if not response.items: raise ValueError('COMMODITY_SOURCE_NOT_AVAILABLE')
    if len(response.items)>=(10000 if api=='fut_basic' else 2000): raise ValueError('COMMODITY_RESPONSE_REJECTED')
    start,end=date.fromisoformat(req['startDate']),date.fromisoformat(req['asOf'])
    result={}; seen_daily=set()
    assets = CONTRACT_ASSETS if catalog is None else catalog.commodities.assets
    asset=next((a for a in assets if a.code==params.get('ts_code') or
        (api=='fut_basic' and a.code.split('.')[0]==params.get('fut_code') and a.exchange==params.get('exchange')) or
        (api=='fut_daily' and monthly_code(params.get('ts_code'),a))),None)
    if api!='fut_trade_cal' and asset is None: raise ValueError('COMMODITY_RESPONSE_REJECTED')
    for values in response.items:
        if len(values)!=len(fields): raise ValueError('COMMODITY_RESPONSE_REJECTED')
        row=dict(zip(response.fields,values,strict=True))
        if api=='fut_basic':
            key=row['ts_code']
            if row['fut_code']!=params['fut_code'] or row['exchange']!=params['exchange'] or not monthly_code(key,asset):
                raise ValueError('COMMODITY_RESPONSE_REJECTED')
        else:
            key=parse_day(row['cal_date' if api=='fut_trade_cal' else 'trade_date']).isoformat()
            if not start<=date.fromisoformat(key)<=end: raise ValueError('COMMODITY_RESPONSE_REJECTED')
            if api=='fut_trade_cal':
                if row['exchange']!=params['exchange'] or type(row['is_open']) not in (str,int) or str(row['is_open']) not in {'0','1'}:
                    raise ValueError('COMMODITY_RESPONSE_REJECTED')
                row['is_open']=int(row['is_open'])
            elif row['ts_code']!=params['ts_code']: raise ValueError('COMMODITY_RESPONSE_REJECTED')
            elif api=='fut_mapping':
                if not monthly_code(row['mapping_ts_code'],asset): raise ValueError('COMMODITY_RESPONSE_REJECTED')
            else:
                if key in seen_daily: raise ValueError('COMMODITY_RESPONSE_REJECTED')
                seen_daily.add(key)
                # 请求覆盖月合约窗口，但可画OHLC只需验证当时真实主力日期。
                # 交割阶段的非主力记录保留Raw，不伪造0蜡烛或污染有效主力日。
                if req.get('activeDates') is not None and key not in req['activeDates']:
                    continue
                for field in fields[2:]: row[field]=number(row[field],positive=field in {'open','high','low','close','pre_settle','settle'})
                if not row['low']<=row['open']<=row['high'] or not row['low']<=row['close']<=row['high']:
                    raise ValueError('COMMODITY_RESPONSE_REJECTED')
        if key in result: raise ValueError('COMMODITY_RESPONSE_REJECTED')
        result[key]=row
    if api=='fut_trade_cal' and set(result)!={(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)}:
        raise ValueError('COMMODITY_RESPONSE_REJECTED')
    if api=='fut_mapping' and end.isoformat() not in result: raise ValueError('COMMODITY_RESPONSE_REJECTED')
    if api=='fut_daily' and req.get('activeDates') is not None and not set(req['activeDates']).issubset(result):
        raise ValueError('COMMODITY_RESPONSE_REJECTED')
    return result


def read_component(session, catalog, req, now, *, fp=None):
    try:
        cursor=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB_NAME,JobCursor.cursor_key==req['identity']))
        proof=cursor.cursor_value
        raw_id=proof['sourcePayloadId']
        if type(raw_id) is not int or raw_id<=0: return None
        raw=session.get(SourcePayload,raw_id)
        if raw is None or raw.api_name!=req['api'] or raw.content_hash!=content_hash(raw.payload) or proof['contentHash']!=raw.content_hash: return None
        if "contractFingerprint" in raw.payload and raw.payload["contractFingerprint"] != (fp or fingerprint(catalog)):
            return None
        captured=aware(raw.captured_at)
        batch=session.get(IngestionBatch,raw.batch_id)
        if (batch is None or batch.source!='tushare' or batch.dataset!=f'market:commodities:{req["api"]}' or batch.status!='completed'
            or batch.finished_at is None or aware(batch.started_at)!=captured or aware(batch.finished_at)!=captured
            or aware(cursor.updated_at)!=captured or captured>now
            or captured.astimezone(ZoneInfo('Asia/Shanghai')).date()<date.fromisoformat(req['asOf'])): return None
        if (raw.payload['commodityRequest']!=req['params'] or raw.payload['commodityAsOf']!=req['asOf']
            or raw.request_hash!=request_hash(req['api'],req['params'],FIELDS[req['api']])): return None
        return {'rows':normalize(req,TushareResponse.model_validate(raw.payload),catalog),
            'sourcePayloadId':raw.id,'capturedAt':captured.isoformat()}
    except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError): return None


def asset_daily_facts(asset, day, source):
    """同一窗口逐日关联期货交易所日历、主力月合约、合约定义及其日线。"""
    calendar = source("fut_trade_cal", exchange=asset.exchange)
    mapping = source("fut_mapping", ts_code=asset.code)
    metadata = source("fut_basic", fut_code=asset.code.split(".")[0])
    open_days = {d for d, row in calendar["rows"].items() if row["is_open"] == 1}
    if set(mapping["rows"]) != open_days or day.isoformat() not in open_days:
        raise ValueError("COMMODITY_CALENDAR_MISMATCH")
    rows = []
    for trade_date, mapped in sorted(mapping["rows"].items()):
        code = mapped["mapping_ts_code"]
        meta = metadata["rows"][code]
        if not parse_day(meta["list_date"]) <= date.fromisoformat(trade_date) <= parse_day(meta["delist_date"]):
            raise ValueError("COMMODITY_CONTRACT_INACTIVE")
        if any(not isinstance(meta[k], str) or not meta[k].strip() for k in ("quote_unit", "trade_unit")):
            raise ValueError("COMMODITY_UNIT_MISSING")
        daily = source("fut_daily", ts_code=code)
        quote = daily["rows"][trade_date]
        components = {"fut_trade_cal": calendar, "fut_mapping": mapping, "fut_basic": metadata, "fut_daily": daily}
        rows.append({
            "asset_code": asset.code, "exchange": asset.exchange, "trade_date": trade_date, "contract": code,
            "quote_unit": meta["quote_unit"], "trade_unit": meta["trade_unit"],
            "per_unit": number(meta["per_unit"], positive=True),
            **{key: quote[key] for key in FIELDS["fut_daily"][2:]},
            "sources": {api: {"source_payload_id": item["sourcePayloadId"], "captured_at": item["capturedAt"]}
                        for api, item in components.items()},
        })
    if len({row["quote_unit"] for row in rows}) != 1:
        raise ValueError("COMMODITY_QUOTE_UNIT_CHANGED")
    return rows


def _asset_window(session, asset, day, start, fp, now):
    cached = {}
    dates = {"start_date": start.strftime("%Y%m%d"), "end_date": day.strftime("%Y%m%d")}

    def source(api, **params):
        key = (api, tuple(params.items()))
        if key in cached:
            return cached[key]
        if api == "fut_basic":
            params = {**params, "exchange": asset.exchange, "fut_type": "1"}
        else:
            params = {**params, **dates}
        req = frozen_request(api, params, fp, start, day)
        if api == "fut_daily":
            mapping = source("fut_mapping", ts_code=asset.code)
            req["activeDates"] = [d for d, row in mapping["rows"].items() if row["mapping_ts_code"] == params["ts_code"]]
        component = read_component(session, None, req, now, fp=fp)
        if component is None:
            raise ValueError("COMMODITY_SOURCE_INCOMPLETE")
        cached[key] = component
        return component

    try:
        return asset_daily_facts(asset, day, source)
    except (KeyError, ValueError, TypeError, StopIteration, ArithmeticError):
        return []


def read_asset_daily(session, catalog, asset_code, day, now, *, requested_start):
    asset = next((a for a in CONTRACT_ASSETS if a.code == asset_code), None)
    if asset is None:
        return []
    raw = SourcePayload
    params = raw.payload["commodityRequest"]
    anchors = session.execute(select(params["start_date"].as_string(), raw.payload["contractFingerprint"].as_string())
        .join(IngestionBatch).where(raw.api_name == "fut_mapping", IngestionBatch.dataset == "market:commodities:fut_mapping",
            IngestionBatch.status == "completed", raw.captured_at <= now, params["ts_code"].as_string() == asset_code,
            params["end_date"].as_string() == day.strftime("%Y%m%d"), raw.payload["commodityAsOf"].as_string() == day.isoformat())
        .distinct()).all()
    legacy = set()
    if any(fp is None for _, fp in anchors):
        legacy = job_fingerprints(session, JOB_NAME, day, now)
        catalog = legacy_candidate_catalog(catalog)
    windows = set()
    for start_text, fp in anchors:
        try:
            start = parse_day(start_text)
        except (TypeError, ValueError):
            continue
        history_days = (day - start).days + 1
        if not 5 <= history_days <= 366:
            continue
        candidates = set(legacy) if fp is None else {fp}
        if fp is None and catalog is not None:
            options = catalog.commodities.model_copy(update={"history_days": history_days})
            candidates.add(fingerprint(catalog.model_copy(update={"commodities": options})))
        windows.update((start, value) for value in candidates if valid_fingerprint(value))
    candidates = []
    for start, fp in sorted(windows):
        facts = _asset_window(session, asset, day, start, fp, now)
        if facts:
            completed = max(proof["captured_at"] for row in facts for proof in row["sources"].values())
            rank = (min((day - start).days, (day - requested_start).days), completed)
            values = {row["trade_date"]:{k:v for k,v in row.items() if k != "sources"} for row in facts}
            candidates.append((rank, values, facts))
    return choose_contract(candidates) or []
