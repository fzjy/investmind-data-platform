"""Raw分量与可恢复来源图；不创建证券、迁移或使用股票行情冒充期货。"""
from datetime import date
from decimal import Decimal

from app.db.models import JobCursor
from app.ingestion.repositories import IngestionRepository,content_hash
from app.market.commodities.contracts import JOB_NAME,FIELDS,fingerprint,request,source_requests,window
from app.market.commodities.aggregation import aggregate
from app.market.commodities.facts import asset_daily_facts, normalize, number, parse_day, read_component


class CommodityRepository:
    def __init__(self,factory,catalog): self.factory,self.catalog=factory,catalog

    def read(self,req,now,session=None):
        if session is None:
            with self.factory() as active: return self.read(req,now,active)
        return read_component(session, self.catalog, req, now)

    def persist(self,req,response,captured_at):
        if captured_at.utcoffset() is None: raise ValueError('COMMODITY_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion=IngestionRepository(session)
            batch=ingestion.start_batch(f'market:commodities:{req["api"]}','live')
            batch.started_at=batch.finished_at=captured_at; batch.request_count=1
            raw=ingestion.save_payload(batch,req['api'],req['params'],FIELDS[req['api']],response)
            raw.payload={**raw.payload,'commodityRequest':req['params'],'commodityAsOf':req['asOf'],
                         'contractFingerprint':fingerprint(self.catalog)}
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
            facts = asset_daily_facts(asset, day, source)
            proofs = [proof for fact in facts for proof in fact['sources'].values()]
            bars = [{'date': fact['trade_date'], 'contract': fact['contract'],
                     **{key: float(fact[key]) for key in ('open', 'high', 'low', 'close', 'settle')},
                     'volume': float(fact['vol']), 'amount': float(fact['amount']),
                     'openInterest': float(fact['oi']), 'preClose': float(fact['pre_close']),
                     'preSettle': float(fact['pre_settle'])} for fact in facts]
            last = bars[-1]
            meta = facts[-1]
            change=(Decimal(str(last['close']))/Decimal(str(bars[-2]['close']))-1)*100 if len(bars)>1 else None
            data[asset.category]['domestic'].append({'dataKind':'futures-ohlc','code':asset.code,'name':asset.name+'主力',
                'activeContract':last['contract'],'price':str(last['close']),'change':float(change) if change is not None else None,'tradeDate':last['date'],
                'currency':'CNY','quoteUnit':meta['quote_unit'],'tradeUnit':meta['trade_unit'],'perUnit':float(number(meta['per_unit'],positive=True)),
                'amountUnit':'wan-yuan','volumeUnit':'hand','changeBasis':'close/previous_series_close-1','adjustment':'unadjusted',
                'source':'tushare:fut_mapping+fut_basic+fut_daily+fut_trade_cal',
                'capturedAtFrom':min(p['captured_at'] for p in proofs),'capturedAtTo':max(p['captured_at'] for p in proofs),
                'sourcePayloadIds':sorted({p['source_payload_id'] for p in proofs}),
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
