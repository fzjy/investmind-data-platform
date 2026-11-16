"""Raw分量与可恢复来源图；不创建证券、迁移或使用股票行情冒充期货。"""
from datetime import date,datetime,timedelta
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.connectors.tushare.contracts import TushareResponse,request_hash
from app.db.models import SourcePayload,IngestionBatch,JobCursor
from app.ingestion.repositories import IngestionRepository,content_hash
from app.market.data.repository import aware
from app.market.commodities.contracts import JOB_NAME,FIELDS,request,source_requests,window,monthly_code
from app.market.commodities.aggregation import aggregate


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
    asset=next((a for a in catalog.commodities.assets if a.code==params.get('ts_code') or
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


class CommodityRepository:
    def __init__(self,factory,catalog): self.factory,self.catalog=factory,catalog

    def read(self,req,now,session=None):
        if session is None:
            with self.factory() as active: return self.read(req,now,active)
        try:
            cursor=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB_NAME,JobCursor.cursor_key==req['identity']))
            proof=cursor.cursor_value
            raw_id=proof['sourcePayloadId']
            if type(raw_id) is not int or raw_id<=0: return None
            raw=session.get(SourcePayload,raw_id)
            if raw is None or raw.api_name!=req['api'] or raw.content_hash!=content_hash(raw.payload) or proof['contentHash']!=raw.content_hash: return None
            captured=aware(raw.captured_at)
            batch=session.get(IngestionBatch,raw.batch_id)
            if (batch is None or batch.source!='tushare' or batch.dataset!=f'market:commodities:{req["api"]}' or batch.status!='completed'
                or batch.finished_at is None or aware(batch.started_at)!=captured or aware(batch.finished_at)!=captured
                or aware(cursor.updated_at)!=captured or captured>now
                or captured.astimezone(ZoneInfo('Asia/Shanghai')).date()<date.fromisoformat(req['asOf'])): return None
            if (raw.payload['commodityRequest']!=req['params'] or raw.payload['commodityAsOf']!=req['asOf']
                or raw.request_hash!=request_hash(req['api'],req['params'],FIELDS[req['api']])): return None
            return {'rows':normalize(req,TushareResponse.model_validate(raw.payload),self.catalog),
                'sourcePayloadId':raw.id,'capturedAt':captured.isoformat()}
        except (ValueError,KeyError,TypeError,AttributeError,ArithmeticError): return None

    def persist(self,req,response,captured_at):
        if captured_at.utcoffset() is None: raise ValueError('COMMODITY_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion=IngestionRepository(session)
            batch=ingestion.start_batch(f'market:commodities:{req["api"]}','live')
            batch.started_at=batch.finished_at=captured_at; batch.request_count=1
            raw=ingestion.save_payload(batch,req['api'],req['params'],FIELDS[req['api']],response)
            raw.payload={**raw.payload,'commodityRequest':req['params'],'commodityAsOf':req['asOf']}
            raw.content_hash=content_hash(raw.payload); raw.captured_at=captured_at
            try: normalize(req,response,self.catalog)
            except (ValueError,TypeError,ArithmeticError) as exc:
                code=str(exc) if str(exc)=='COMMODITY_SOURCE_NOT_AVAILABLE' else 'COMMODITY_RESPONSE_REJECTED'
                batch.status,batch.rejected_count='rejected',len(response.items)
                ingestion.reject(batch,code)
                return code
            batch.status,batch.row_count='completed',len(response.items)
            ingestion.upsert(JobCursor,{'job_name':JOB_NAME,'cursor_key':req['identity']},
                {'cursor_value':{'sourcePayloadId':raw.id,'contentHash':raw.content_hash},'updated_at':captured_at})
            from app.acquisition.adapters.markets import record_market
            record_market(session,req['api'],req['params'].get('ts_code') or req['params'].get('exchange'),req['params'],captured_at,batch_id=batch.id)
            return None

    def plan(self,day,now,session=None):
        if session is None:
            with self.factory() as active: return self.plan(day,now,active)
        requests=source_requests(self.catalog,day)
        cached={r['identity']:self.read(r,now,session) for r in requests}
        for r in list(requests):
            if r['api']=='fut_mapping' and cached[r['identity']] is not None:
                for code in sorted({row['mapping_ts_code'] for row in cached[r['identity']]['rows'].values()}):
                    extra=request('fut_daily',{'ts_code':code,'start_date':window(self.catalog,day).strftime('%Y%m%d'),'end_date':day.strftime('%Y%m%d')},self.catalog,day)
                    extra['activeDates']=sorted(d for d,row in cached[r['identity']]['rows'].items() if row['mapping_ts_code']==code)
                    requests.append(extra); cached[extra['identity']]=self.read(extra,now,session)
        # 单份Raw合法不等于来源关系完整；将可修复的冲突重新列为待补，不清游标。
        invalid=set()
        for asset in self.catalog.commodities.assets:
            calendar_req=next(r for r in requests if r['api']=='fut_trade_cal' and r['params']['exchange']==asset.exchange)
            mapping_req=next(r for r in requests if r['api']=='fut_mapping' and r['params']['ts_code']==asset.code)
            meta_req=next(r for r in requests if r['api']=='fut_basic' and r['params']['fut_code']==asset.code.split('.')[0])
            calendar,mapping,meta=(cached[r['identity']] for r in (calendar_req,mapping_req,meta_req))
            if calendar and mapping:
                expected={d for d,row in calendar['rows'].items() if row['is_open']==1}
                if set(mapping['rows'])!=expected: invalid.add(mapping_req['identity'])
            if mapping and meta:
                units=set()
                for d,mapped in mapping['rows'].items():
                    code=mapped['mapping_ts_code']
                    try:
                        row=meta['rows'][code]
                        if not parse_day(row['list_date'])<=date.fromisoformat(d)<=parse_day(row['delist_date']): raise ValueError()
                        if not isinstance(row['quote_unit'],str) or not row['quote_unit'].strip() or not isinstance(row['trade_unit'],str) or not row['trade_unit'].strip(): raise ValueError()
                        number(row['per_unit'],positive=True); units.add(row['quote_unit'])
                    except (KeyError,ValueError,TypeError): invalid.add(meta_req['identity'])
                    daily_req=next(r for r in requests if r['api']=='fut_daily' and r['params']['ts_code']==code)
                    daily=cached[daily_req['identity']]
                    if daily and d not in daily['rows']: invalid.add(daily_req['identity'])
                if len(units)>1: invalid.add(meta_req['identity'])
        for key in invalid: cached[key]=None
        pending=[r for r in requests if cached[r['identity']] is None]
        ready=False
        if not pending:
            ready=_build(self.catalog,day,requests,cached) is not None
        return {'pendingRequests':pending,'pendingCount':len(pending),'discoveredRequestCount':len(requests),'ready':ready,
            'requests':requests,'cached':cached}


def _build(catalog,day,requests,cached):
    try:
        def source(api,**params):
            req=next(r for r in requests if r['api']==api and all(r['params'].get(k)==v for k,v in params.items()))
            return cached[req['identity']]
        data={key:{'domestic':[],'overseas':[]} for key in ('precious','nonferrous','ferrous','energy','agriculture')}
        for asset in catalog.commodities.assets:
            calendar=source('fut_trade_cal',exchange=asset.exchange)
            expected={d for d,row in calendar['rows'].items() if row['is_open']==1}
            mapping=source('fut_mapping',ts_code=asset.code)
            if set(mapping['rows'])!=expected or day.isoformat() not in expected: return None
            metadata=source('fut_basic',fut_code=asset.code.split('.')[0])
            bars=[]; proofs=[calendar,mapping,metadata]
            for d,mapped in sorted(mapping['rows'].items()):
                code=mapped['mapping_ts_code']; meta=metadata['rows'][code]
                if not parse_day(meta['list_date'])<=date.fromisoformat(d)<=parse_day(meta['delist_date']): return None
                if (not isinstance(meta['quote_unit'],str) or not meta['quote_unit'].strip()
                    or not isinstance(meta['trade_unit'],str) or not meta['trade_unit'].strip()): return None
                number(meta['per_unit'],positive=True)
                daily=source('fut_daily',ts_code=code); row=daily['rows'][d]
                proofs.append(daily)
                bars.append({'date':d,'contract':code,**{k:float(row[k]) for k in ('open','high','low','close')},
                    'volume':float(row['vol']),'amount':float(row['amount']),'openInterest':float(row['oi']),
                    'preClose':float(row['pre_close']),'preSettle':float(row['pre_settle']),'settle':float(row['settle'])})
            last=bars[-1]; meta=metadata['rows'][last['contract']]
            if len({metadata['rows'][bar['contract']]['quote_unit'] for bar in bars})!=1: return None
            change=(Decimal(str(last['close']))/Decimal(str(bars[-2]['close']))-1)*100 if len(bars)>1 else None
            data[asset.category]['domestic'].append({'dataKind':'futures-ohlc','code':asset.code,'name':asset.name+'主力',
                'activeContract':last['contract'],'price':str(last['close']),'change':float(change) if change is not None else None,'tradeDate':last['date'],
                'currency':'CNY','quoteUnit':meta['quote_unit'],'tradeUnit':meta['trade_unit'],'perUnit':float(number(meta['per_unit'],positive=True)),
                'amountUnit':'wan-yuan','volumeUnit':'hand','changeBasis':'close/previous_series_close-1','adjustment':'unadjusted',
                'source':'tushare:fut_mapping+fut_basic+fut_daily+fut_trade_cal',
                'capturedAtFrom':min(p['capturedAt'] for p in proofs),'capturedAtTo':max(p['capturedAt'] for p in proofs),
                'sourcePayloadIds':sorted({p['sourcePayloadId'] for p in proofs}),
                'bars':{'intraday':[],'daily':bars,'weekly':aggregate(bars,'weekly',window(catalog,day),day),
                    'monthly':aggregate(bars,'monthly',window(catalog,day),day)}})
        return {'commodityCategories':[{'id':k,'label':v} for k,v in [('precious','贵金属'),('nonferrous','有色金属'),('ferrous','黑色系'),('energy','能源化工'),('agriculture','农产品')]],
            'commodityPeriods':[{'id':k,'label':v} for k,v in [('daily','日线'),('weekly','周线'),('monthly','月线')]],
            'commodityData':data,'commodityMetadata':{'asOf':day.isoformat(),'windowStart':window(catalog,day).isoformat(),
                'instrumentCount':10,'adjustment':'unadjusted','overseasStatus':'not_connected','intradayStatus':'not_connected'}}
    except (KeyError,ValueError,TypeError,StopIteration,ArithmeticError): return None


def package(session,catalog,day,now):
    plan=CommodityRepository(None,catalog).plan(day,now,session)
    return _build(catalog,day,plan['requests'],plan['cached']) if not plan['pendingRequests'] else None
