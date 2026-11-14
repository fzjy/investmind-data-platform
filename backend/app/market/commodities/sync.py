from datetime import date
from uuid import uuid4

from app.connectors.tushare.errors import TushareError,TushareAuthError,TusharePermissionError
from app.db.models import JobRun,JobFailure
from app.market.pulse.repository import PulseRepository
from app.market.commodities.contracts import JOB_NAME,FIELDS,fingerprint
from app.market.commodities.repository import CommodityRepository
from app.market.commodities.publication import publish_commodity_marts


class CommoditySync:
    def __init__(self,factory,client,selection,catalog,lock,*,clock=None):
        self.factory,self.client,self.selection,self.catalog,self.lock=factory,client,selection,catalog,lock
        self.clock=clock

    def preflight(self,now):
        if now.utcoffset() is None: raise ValueError('COMMODITY_AWARE_CLOCK_REQUIRED')
        report={'status':'planned','requestCount':0,'readOnly':True,'publishReady':False,'pendingCount':0,
            'httpLimit':self.catalog.commodities.max_requests_per_run,'universe':self.selection.to_payload()}
        if not self.catalog.enabled or not self.catalog.commodities.enabled: return {**report,'status':'disabled'}
        day=PulseRepository(self.factory).target(self.selection,now)
        plan=CommodityRepository(self.factory,self.catalog).plan(day,now)
        return {**report,'asOf':day.isoformat(),'pendingRequests':plan['pendingRequests'],'pendingCount':plan['pendingCount'],
            'discoveredRequestCount':plan['discoveredRequestCount'],'publishReady':plan['ready']}

    def run(self,now,*,publish=False):
        held=[]; requests=retained=0; run_id=None
        report={'status':'failed','requestCount':0,'publishReady':False}
        try:
            for key in (JOB_NAME,'sync-stock-data:representative-50'):
                if self.lock is None or not self.lock.try_acquire(key):
                    report={'status':'locked','code':'COMMODITY_LOCK_BUSY','requestCount':0}; return report
                held.append(key)
            report=self.preflight(now)
            if report['status']!='planned': return report
            day=date.fromisoformat(report['asOf']); repo=CommodityRepository(self.factory,self.catalog)
            attempted=set(); errors=[]
            if report['pendingCount']:
                if self.client is None: raise ValueError('COMMODITY_CREDENTIAL_REQUIRED')
                run_id=str(uuid4())
                with self.factory.begin() as session:
                    session.add(JobRun(id=run_id,job_name=JOB_NAME,business_key=f'{fingerprint(self.catalog)}:{day}',status='running',started_at=now,details={}))
                def before_request():
                    nonlocal requests
                    if requests>=self.catalog.commodities.max_requests_per_run: raise ValueError('COMMODITY_HTTP_BUDGET_EXHAUSTED')
                    requests+=1
                self.client.set_before_request(before_request)
                try:
                    while requests<self.catalog.commodities.max_requests_per_run:
                        checked=self.clock() if self.clock else now
                        plan=repo.plan(day,checked)
                        pending=[r for r in plan['pendingRequests'] if r['identity'] not in attempted]
                        if not pending: break
                        req=pending[0]; attempted.add(req['identity']); error=None; stop=False
                        try:
                            data=self.client.call(req['api'],req['params'],FIELDS[req['api']])
                            captured=self.clock() if self.clock else now
                            if captured.utcoffset() is None or captured<now: raise ValueError('COMMODITY_CLOCK_REGRESSED')
                            error=repo.persist(req,data,captured); retained+=int(error is None)
                        except (TushareAuthError,TusharePermissionError): error,stop='COMMODITY_SOURCE_PERMISSION_OR_AUTH_REQUIRED',True
                        except TushareError as exc: error=type(exc).__name__.upper()
                        except Exception: error,stop='COMMODITY_SYNC_FAILED',True
                        if error:
                            errors.append(error)
                            try:
                                with self.factory.begin() as session:
                                    session.add(JobFailure(job_run_id=run_id,dataset=JOB_NAME,request_key=req['identity'],error_code=error,
                                        message=error,request_payload={'api':req['api'],'params':req['params']},retryable=not stop,last_attempted_at=now))
                            except Exception: errors.append('COMMODITY_FAILURE_RECORD_UNAVAILABLE'); stop=True
                        if stop: break
                except Exception: errors.append('COMMODITY_PLAN_RECHECK_FAILED')
            report.update(readOnly=False,requestCount=requests,retainedCount=retained)
            verified=now
            try:
                verified=self.clock() if self.clock else now
                if verified.utcoffset() is None or verified<now: raise ValueError('COMMODITY_CLOCK_REGRESSED')
                checked=self.preflight(verified)
                ready=checked.get('asOf')==day.isoformat() and checked.get('publishReady',False)
                report.update(status='completed' if ready else 'partial' if retained else 'failed',publishReady=ready,
                    pendingCount=checked.get('pendingCount'),pendingRequests=checked.get('pendingRequests'),
                    discoveredRequestCount=checked.get('discoveredRequestCount'))
                if checked.get('asOf')!=day.isoformat(): errors.append('COMMODITY_TARGET_CHANGED')
                if not ready and requests>=self.catalog.commodities.max_requests_per_run: errors.append('COMMODITY_HTTP_BUDGET_EXHAUSTED')
            except Exception:
                report.update(status='failed',code='COMMODITY_RECHECK_FAILED',publishReady=False,pendingCount=None,pendingRequests=None)
                errors.append('COMMODITY_RECHECK_FAILED')
            report['errorCodes']=sorted(set(errors))
            if run_id:
                try:
                    with self.factory.begin() as session:
                        row=session.get(JobRun,run_id)
                        row.status,row.request_count,row.row_count,row.finished_at=report['status'],requests,retained,verified
                        row.details={'asOf':day.isoformat(),'pendingCount':report['pendingCount'],'errorCodes':report['errorCodes']}
                except Exception: report.update(status='failed',publishReady=False,code='COMMODITY_RUN_RECORD_UNAVAILABLE')
            if publish and report['publishReady']:
                try: result=publish_commodity_marts(self.factory,self.selection,self.catalog,checked_at=verified,expected_as_of=day)
                except Exception: result={'status':'failed','code':'COMMODITY_PUBLICATION_FAILED'}
                report['publication']=result
                if result['status'] not in {'completed','no_change'}: report['status']='partial'
            return report
        finally:
            for key in reversed(held):
                try: self.lock.release(key)
                except Exception: report.update(status='failed',cleanupError='COMMODITY_UNLOCK_FAILED',requestCount=requests)
