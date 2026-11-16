"""九ETF、两个分量独立检查点，有限请求后成对发布市场。"""
from datetime import date
from uuid import uuid4

from app.connectors.tushare.errors import TushareError, TushareAuthError, TusharePermissionError
from app.db.models import JobRun, JobFailure
from app.market.etf.repository import EtfRepository, JOB_NAME, FIELDS, parameters, fingerprint, identity, quotes, tiles
from app.market.etf.publication import publish_etf_marts


class EtfSync:
    def __init__(self,factory,client,selection,catalog,lock,*,clock=None):
        self.factory,self.client,self.selection,self.catalog,self.lock=factory,client,selection,catalog,lock
        self.clock=clock
        self.fingerprint=fingerprint(catalog)

    def preflight(self,now):
        if now.utcoffset() is None:
            raise ValueError('ETF_AWARE_CLOCK_REQUIRED')
        report={'status':'planned','readOnly':True,'requestCount':0,'httpLimit':self.catalog.etf_heatmap.max_requests_per_run,
            'publishReady':False,'universe':self.selection.to_payload(),'pendingCount':0}
        if not self.catalog.enabled or not self.catalog.etf_heatmap.enabled:
            return {**report,'status':'disabled'}
        repo=EtfRepository(self.factory)
        day=repo.target(self.selection,now)
        with self.factory() as session:
            valid_quotes=quotes(session,self.catalog,day,now)
        assets=[a for a in self.catalog.assets if a.asset_type=='etfs']
        if set(valid_quotes)!={a.asset_code for a in assets}:
            return {**report,'status':'blocked','code':'ETF_VALIDATED_QUOTES_REQUIRED','asOf':day.isoformat()}
        pending=[]
        for asset in assets:
            for api in FIELDS:
                component=repo.component(asset.asset_code,day,api,self.fingerprint,now)
                quote=valid_quotes[asset.asset_code]
                if component is None or (api=='fund_daily' and
                    (component['close']!=quote.close or component['pct_chg']!=quote.pct_chg)):
                    pending.append({'code':asset.asset_code,'api':api})
        with self.factory() as session:
            ready=bool(tiles(session,self.catalog,day,now))
        return {**report,'asOf':day.isoformat(),'pending':pending,'pendingCount':len(pending),'publishReady':ready}

    def run(self,now,*,publish=False):
        held=[]
        report={'status':'failed','requestCount':0,'publishReady':False}
        requests=retained=0
        try:
            # 全部是非阻塞锁；忙时退出，不等待造成锁序死锁。
            for key in (JOB_NAME,'sync-stock-data:representative-50','sync-market-data'):
                if self.lock is None or not self.lock.try_acquire(key):
                    report={'status':'locked','code':'ETF_LOCK_BUSY','requestCount':0}
                    return report
                held.append(key)
            report=self.preflight(now)
            if report['status']!='planned':
                return report
            day=date.fromisoformat(report['asOf'])
            repo=EtfRepository(self.factory)
            errors=[]
            run_id=None
            if report['pendingCount']:
                if self.client is None:
                    raise ValueError('ETF_CREDENTIAL_REQUIRED')
                run_id=str(uuid4())
                with self.factory.begin() as session:
                    session.add(JobRun(id=run_id,job_name=JOB_NAME,business_key=f'{self.fingerprint}:{day}',status='running',started_at=now,details={}))
                def before_request():
                    nonlocal requests
                    if requests>=self.catalog.etf_heatmap.max_requests_per_run:
                        raise ValueError('ETF_HTTP_BUDGET_EXHAUSTED')
                    requests+=1
                self.client.set_before_request(before_request)
                for item in report['pending']:
                    if requests>=self.catalog.etf_heatmap.max_requests_per_run:
                        errors.append('ETF_HTTP_BUDGET_EXHAUSTED')
                        break
                    code,api=item['code'],item['api']
                    error,stop=None,False
                    try:
                        response=self.client.call(api,parameters(code,day),FIELDS[api])
                        captured=self.clock() if self.clock else now
                        if captured<now:
                            raise ValueError('ETF_CLOCK_REGRESSED')
                        error=repo.persist(code,day,api,self.fingerprint,response,captured)
                        retained+=int(error is None)
                    except (TushareAuthError,TusharePermissionError):
                        error,stop='ETF_SOURCE_PERMISSION_OR_AUTH_REQUIRED',True
                    except TushareError as exc:
                        error=type(exc).__name__.upper()
                    except Exception:
                        error,stop='ETF_SYNC_FAILED',True
                    if error:
                        errors.append(error)
                        try:
                            with self.factory.begin() as session:
                                from app.acquisition.adapters.markets import record_market
                                record_market(session,api,code,parameters(code,day),now,error=error,run_id=run_id)
                                session.add(JobFailure(job_run_id=run_id,dataset=JOB_NAME,request_key=identity(code,day,api,self.fingerprint),
                                    error_code=error,message=error,request_payload={'tsCode':code,'tradeDate':day.isoformat(),'api':api},
                                    retryable=not stop,last_attempted_at=now))
                        except Exception:
                            errors.append('ETF_FAILURE_RECORD_UNAVAILABLE')
                            stop=True
                    if stop:
                        break
            report.update(readOnly=False,requestCount=requests,retainedCount=retained)
            try:
                verified=self.clock() if self.clock else now
                if verified.utcoffset() is None or verified<now:
                    raise ValueError('ETF_CLOCK_REGRESSED')
                checked=self.preflight(verified)
            except Exception:
                report.update(status='failed',code='ETF_RECHECK_FAILED',publishReady=False,pending=None,pendingCount=None)
                if run_id:
                    try:
                        with self.factory.begin() as session:
                            row=session.get(JobRun,run_id)
                            row.status,row.request_count,row.row_count,row.finished_at='failed',requests,retained,now
                            row.details={'asOf':day.isoformat(),'errorCodes':['ETF_RECHECK_FAILED']}
                    except Exception:
                        report['runRecordError']='ETF_RUN_RECORD_UNAVAILABLE'
                return report
            same_target=checked.get('asOf')==day.isoformat()
            ready=same_target and checked.get('publishReady',False)
            pending=checked.get('pending',report['pending'])
            status='completed' if ready else 'partial' if retained or len(pending)<18 else 'failed'
            if not same_target:
                errors.append('ETF_TARGET_CHANGED')
            elif not pending and not ready:
                errors.append('ETF_QUOTE_MISMATCH')
            report.update(status=status,readOnly=False,requestCount=requests,retainedCount=retained,
                pending=pending,pendingCount=len(pending),publishReady=ready,errorCodes=sorted(set(errors)))
            if run_id:
                try:
                    with self.factory.begin() as session:
                        row=session.get(JobRun,run_id)
                        row.status,row.request_count,row.row_count,row.finished_at=status,requests,retained,verified
                        row.details={'asOf':day.isoformat(),'pending':pending,'errorCodes':report['errorCodes']}
                except Exception:
                    report.update(status='failed',publishReady=False,code='ETF_RUN_RECORD_UNAVAILABLE')
            if publish and report['publishReady']:
                try:
                    result=publish_etf_marts(self.factory,self.selection,self.catalog,checked_at=verified,expected_as_of=day)
                except Exception:
                    result={'status':'failed','code':'ETF_PUBLICATION_FAILED'}
                report['publication']=result
                if result['status'] not in {'completed','no_change'}:
                    report['status']='partial'
            return report
        finally:
            for key in reversed(held):
                try:
                    self.lock.release(key)
                except Exception:
                    report.update(status='failed',cleanupError='ETF_UNLOCK_FAILED',requestCount=requests)
