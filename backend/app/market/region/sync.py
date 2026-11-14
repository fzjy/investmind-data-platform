"""逐股证券资料补齐，元数据周期独立于行情交易日。"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import uuid4

from app.connectors.tushare.errors import TushareError, TushareAuthError, TusharePermissionError
from app.db.models import JobRun, JobFailure
from app.market.region.repository import RegionRepository, JOB_NAME, FIELDS, parameters
from app.market.marts.market_runtime import rebuild_market_marts

STOCK_LOCK = 'sync-stock-data:representative-50'


class RegionSync:
    def __init__(self, factory, client, selection, catalog, lock, *, clock=None):
        self.factory, self.client, self.selection, self.catalog, self.lock = factory, client, selection, catalog, lock
        self.clock = clock
        contract = {'version':1, 'pool':selection.to_payload(), 'api':'stock_basic', 'fields':FIELDS}
        self.fingerprint = hashlib.sha256(json.dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def preflight(self, now):
        if now.utcoffset() is None:
            raise ValueError('REGION_AWARE_CLOCK_REQUIRED')
        options = self.catalog.region
        base = {'status':'planned','readOnly':True,'requestCount':0,'httpLimit':options.max_requests_per_run,
            'refreshIntervalDays':options.refresh_interval_days,'publishReady':False,'universe':self.selection.to_payload()}
        if not self.catalog.enabled or not options.enabled:
            return {**base,'status':'disabled','pendingCount':0}
        repo = RegionRepository(self.factory)
        day = repo.target(self.selection,now)
        completed = [code for code in self.selection.symbols if repo.is_complete(code,self.fingerprint,now,options.refresh_interval_days)]
        pending = [code for code in self.selection.symbols if code not in completed]
        return {**base,'asOf':day.isoformat(),'completedSymbols':completed,'pendingSymbols':pending,
            'pendingCount':len(pending),'publishReady':not pending}

    def run(self, now, *, publish=False):
        held = []
        report = {'status':'failed','requestCount':0}
        try:
            for key in (JOB_NAME, STOCK_LOCK):
                if self.lock is None or not self.lock.try_acquire(key):
                    report.update(status='locked',code='REGION_LOCK_BUSY')
                    return report
                held.append(key)
            report = self.preflight(now)
            if report['status']=='disabled':
                return report
            repo = RegionRepository(self.factory)
            completed = list(report['completedSymbols'])
            day = date.fromisoformat(report['asOf'])
            errors, requests, retained, run_id = [], 0, 0, None
            if report['pendingCount']:
                if self.client is None:
                    raise ValueError('REGION_CREDENTIAL_REQUIRED')
                run_id = str(uuid4())
                with self.factory.begin() as session:
                    session.add(JobRun(id=run_id,job_name=JOB_NAME,business_key=self.fingerprint,
                        status='running',started_at=now,details={}))
                def before_request():
                    nonlocal requests
                    if requests >= self.catalog.region.max_requests_per_run:
                        raise ValueError('REGION_HTTP_BUDGET_EXHAUSTED')
                    requests += 1
                self.client.set_before_request(before_request)
                for code in report['pendingSymbols']:
                    if requests >= self.catalog.region.max_requests_per_run:
                        errors.append('REGION_HTTP_BUDGET_EXHAUSTED')
                        break
                    stop, error = False, None
                    try:
                        response = self.client.call('stock_basic',parameters(code),FIELDS)
                        captured_at = self.clock() if self.clock is not None else now
                        if repo.persist(code,self.fingerprint,response,captured_at):
                            completed.append(code)
                            retained += 1
                        else:
                            error = 'REGION_RESPONSE_REJECTED'
                    except (TushareAuthError,TusharePermissionError):
                        error, stop = 'REGION_SOURCE_PERMISSION_OR_AUTH_REQUIRED', True
                    except TushareError as exc:
                        error = type(exc).__name__.upper()
                    except Exception:
                        error, stop = 'REGION_SYNC_FAILED', True
                    if error:
                        errors.append(error)
                        try:
                            with self.factory.begin() as session:
                                session.add(JobFailure(job_run_id=run_id,dataset=JOB_NAME,
                                    request_key=repo.identity(code,self.fingerprint),error_code=error,message=error,
                                    request_payload={'tsCode':code},retryable=not stop,last_attempted_at=now))
                        except Exception:
                            errors.append('REGION_FAILURE_RECORD_UNAVAILABLE')
                            stop = True
                    if stop:
                        break
            verified_at = self.clock() if self.clock is not None else now
            try:
                verified = [code for code in self.selection.symbols
                    if repo.is_complete(code,self.fingerprint,verified_at,self.catalog.region.refresh_interval_days)]
                if set(completed) - set(verified):
                    errors.append('REGION_CORE_VALIDATION_FAILED')
                completed = verified
            except Exception:
                completed = []
                errors.append('REGION_FINAL_VALIDATION_UNAVAILABLE')
            pending = [code for code in self.selection.symbols if code not in completed]
            status = 'completed' if not pending else 'partial' if completed else 'failed'
            report.update(status=status,readOnly=False,requestCount=requests,retainedCount=retained,
                completedSymbols=completed,pendingSymbols=pending,pendingCount=len(pending),publishReady=not pending,
                errorCodes=sorted(set(errors)))
            if run_id:
                try:
                    with self.factory.begin() as session:
                        row = session.get(JobRun,run_id)
                        row.status,row.request_count,row.row_count,row.finished_at = status,requests,retained,self.clock() if self.clock is not None else now
                        row.details = {'asOf':day.isoformat(),'completedSymbols':completed,'pendingSymbols':pending,
                            'errorCodes':report['errorCodes']}
                except Exception:
                    report.update(status='failed',publishReady=False,code='REGION_RUN_RECORD_UNAVAILABLE')
            if publish and report['publishReady']:
                try:
                    publication_time = verified_at  # 整池证明采用同一真实检查截止时点。
                    if repo.target(self.selection,publication_time)!=day:
                        result = {'status':'blocked','code':'REGION_TARGET_CHANGED'}
                    else:
                        result = rebuild_market_marts(self.factory,self.selection,checked_at=publication_time,dry_run=False,
                            expected_as_of=day,timezone_name=self.catalog.schedule.timezone,
                            sync_hour=self.catalog.schedule.time.hour,sync_minute=self.catalog.schedule.time.minute)
                except Exception:
                    result = {'status':'failed','code':'REGION_PUBLICATION_FAILED'}
                report['publication'] = result
                if result['status'] not in {'completed','no_change'}:
                    report['status'] = 'partial'
            return report
        finally:
            for key in reversed(held):
                try:
                    self.lock.release(key)
                except Exception:
                    report.update(status='failed',cleanupError='REGION_UNLOCK_FAILED')
