"""逐股断点补齐；与冻结股票源合同完全分离。"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from app.connectors.tushare.errors import TushareError, TushareAuthError, TusharePermissionError
from app.db.models import JobRun, JobFailure
from app.market.pulse.repository import PulseRepository, JOB_NAME, FIELDS, parameters
from app.market.marts.market_runtime import rebuild_market_marts

STOCK_LOCK = 'sync-stock-data:representative-50'


class PulseSync:
    def __init__(self, factory, client, selection, catalog, lock):
        self.factory, self.client, self.selection, self.catalog, self.lock = factory, client, selection, catalog, lock
        contract = {'version': 1, 'pool': selection.to_payload(),
            'pulse': catalog.pulse.model_dump(mode='json', by_alias=True)}
        self.fingerprint = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def preflight(self, now):
        if now.utcoffset() is None:
            raise ValueError('PULSE_AWARE_CLOCK_REQUIRED')
        base = {'status': 'planned', 'readOnly': True, 'requestCount': 0,
            'httpLimit': self.catalog.pulse.max_requests_per_run, 'publishReady': False,
            'universe': self.selection.to_payload(),
            'unavailable': ['BURST_SOURCE_PERMISSION_REQUIRED', 'SENTIMENT_SCORE_NOT_COMPUTED']}
        if not self.catalog.enabled or not self.catalog.pulse.enabled:
            return {**base, 'status': 'disabled', 'pendingCount': 0}
        repo = PulseRepository(self.factory)
        day = repo.target(self.selection, now)
        completed = [code for code in self.selection.symbols if repo.is_complete(code, day, self.fingerprint, now)]
        pending = [code for code in self.selection.symbols if code not in completed]
        return {**base, 'asOf': day.isoformat(), 'completedSymbols': completed, 'pendingSymbols': pending,
            'pendingCount': len(pending), 'publishReady': not pending}

    def run(self, now, *, publish=False):
        held = []
        try:
            for key in (JOB_NAME, STOCK_LOCK):
                if self.lock is None or not self.lock.try_acquire(key):
                    return {'status': 'locked', 'code': 'PULSE_LOCK_BUSY', 'requestCount': 0}
                held.append(key)
            report = self.preflight(now)
            if report['status'] == 'disabled':
                return report
            from datetime import date
            day = date.fromisoformat(report['asOf'])
            completed = list(report['completedSymbols'])
            errors = []
            requests = retained = 0
            run_id = None
            repo = PulseRepository(self.factory)
            if report['pendingCount']:
                if self.client is None:
                    raise ValueError('PULSE_CREDENTIAL_REQUIRED')
                run_id = str(uuid4())
                with self.factory.begin() as session:
                    session.add(JobRun(id=run_id, job_name=JOB_NAME, business_key=f'{self.fingerprint}:{day}',
                        status='running', started_at=now, details={}))

                def before_request():
                    nonlocal requests
                    if requests >= self.catalog.pulse.max_requests_per_run:
                        raise ValueError('PULSE_HTTP_BUDGET_EXHAUSTED')
                    requests += 1

                self.client.set_before_request(before_request)
                for code in report['pendingSymbols']:
                    if requests >= self.catalog.pulse.max_requests_per_run:
                        errors.append('PULSE_HTTP_BUDGET_EXHAUSTED')
                        break
                    stop = False
                    error = None
                    try:
                        response = self.client.call('daily_basic', parameters(code, day), FIELDS)
                        if repo.persist(code, day, self.fingerprint, response, now):
                            completed.append(code)
                            retained += 1
                        else:
                            error = 'PULSE_RESPONSE_REJECTED'
                    except (TushareAuthError, TusharePermissionError):
                        error, stop = 'PULSE_SOURCE_PERMISSION_OR_AUTH_REQUIRED', True
                    except TushareError as exc:
                        error = type(exc).__name__.upper()
                    except Exception:
                        error, stop = 'PULSE_SYNC_FAILED', True
                    if error:
                        errors.append(error)
                        try:
                            with self.factory.begin() as session:
                                session.add(JobFailure(job_run_id=run_id, dataset=JOB_NAME,
                                    request_key=repo.identity(code, day, self.fingerprint), error_code=error,
                                    message=error, request_payload={'tsCode': code, 'tradeDate': day.isoformat()},
                                    retryable=not stop, last_attempted_at=now))
                        except Exception:
                            errors.append('PULSE_FAILURE_RECORD_UNAVAILABLE')
                            stop = True
                    if stop:
                        break
            pending = [code for code in self.selection.symbols if code not in completed]
            status = 'completed' if not pending else 'partial' if completed else 'failed'
            report.update(status=status, readOnly=False, requestCount=requests, retainedCount=retained,
                completedSymbols=completed, pendingSymbols=pending, pendingCount=len(pending),
                publishReady=not pending, errorCodes=sorted(set(errors)))
            if run_id:
                try:
                    with self.factory.begin() as session:
                        row = session.get(JobRun, run_id)
                        row.status, row.request_count, row.row_count, row.finished_at = status, requests, retained, now
                        row.details = {'asOf': day.isoformat(), 'completedSymbols': completed,
                            'pendingSymbols': pending, 'errorCodes': report['errorCodes']}
                except Exception:
                    report.update(status='failed', publishReady=False, code='PULSE_RUN_RECORD_UNAVAILABLE')
            if publish and report['publishReady']:
                try:
                    if repo.target(self.selection, now) != day:
                        result = {'status': 'blocked', 'code': 'PULSE_TARGET_CHANGED'}
                    else:
                        result = rebuild_market_marts(self.factory, self.selection, checked_at=now, dry_run=False,
                            expected_as_of=day, timezone_name=self.catalog.schedule.timezone,
                            sync_hour=self.catalog.schedule.time.hour, sync_minute=self.catalog.schedule.time.minute)
                except Exception:
                    # 收集已经提交；发布失败不能抹去真实HTTP及成功断点。
                    result = {'status': 'failed', 'code': 'PULSE_PUBLICATION_FAILED'}
                report['publication'] = result
                if result['status'] not in {'completed', 'no_change'}:
                    report['status'] = 'partial'
            return report
        finally:
            for key in reversed(held):
                self.lock.release(key)
