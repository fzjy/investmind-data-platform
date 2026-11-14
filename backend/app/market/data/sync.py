from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from app.connectors.tushare.errors import TushareError
from app.db.models import JobRun, JobFailure
from app.jobs.run_errors import JobLockedError
from app.market.data.catalog import resolve_market_targets
from app.market.data.contracts import MarketSyncReport, QUOTE_FIELDS
from app.market.data.repository import JOB_NAME, MarketQuoteRepository


class MarketDataSync:
    def __init__(self, factory, client, catalog, lock, *, targets=None, planned_at=None):
        self.factory = factory
        self.client = client
        self.catalog = catalog
        self.lock = lock
        self.targets = targets
        self.planned_at = planned_at

    def preflight(self, checked_at: datetime) -> dict:
        targets = resolve_market_targets(self.catalog, checked_at, self.targets, planned_at=self.planned_at)
        if not self.catalog.enabled:
            return {'status': 'disabled', 'requestCount': 0, 'pendingCount': 0, 'targets': []}
        repo = MarketQuoteRepository(self.factory)
        rows = [{'assetCode': t.asset.asset_code, 'identity': t.identity,
            'startDate': t.start_date.isoformat(), 'endDate': t.end_date.isoformat(),
            'completed': repo.is_complete(t, checked_at=checked_at)} for t in targets]
        return {'status': 'planned', 'readOnly': True, 'requestCount': 0,
            'pendingCount': sum(not row['completed'] for row in rows), 'targets': rows,
            'httpLimit': self.catalog.sync.max_requests_per_run}

    def run(self, checked_at: datetime) -> MarketSyncReport:
        targets = resolve_market_targets(self.catalog, checked_at, self.targets, planned_at=self.planned_at)
        if not self.catalog.enabled:
            return MarketSyncReport('disabled')
        if self.lock is None or not self.lock.try_acquire(JOB_NAME):
            raise JobLockedError('MARKET_SYNC_LOCK_BUSY')
        requests = retained = 0
        completed, pending, errors = [], [], []
        run_id = str(uuid4())
        try:
            repo = MarketQuoteRepository(self.factory)
            # 先检查表/数据合同；缺迁移不留下半个running作业。
            cache = {t.identity: repo.is_complete(t, checked_at=checked_at) for t in targets}
            if all(cache.values()):
                return MarketSyncReport('completed', completed_assets=tuple(t.asset.asset_code for t in targets))
            if self.client is None:
                raise ValueError('MARKET_CREDENTIAL_REQUIRED')
            with self.factory.begin() as session:
                session.add(JobRun(id=run_id, job_name=JOB_NAME, business_key=self.catalog.fingerprint,
                    status='running', started_at=checked_at, details={}))

            def before_request():
                nonlocal requests
                if requests >= self.catalog.sync.max_requests_per_run:
                    raise ValueError('MARKET_HTTP_BUDGET_EXHAUSTED')
                requests += 1

            self.client.set_before_request(before_request)
            for t in targets:
                code = t.asset.asset_code
                if cache[t.identity]:
                    completed.append(code)
                    continue
                if requests >= self.catalog.sync.max_requests_per_run:
                    pending.append(code)
                    if 'MARKET_HTTP_BUDGET_EXHAUSTED' not in errors:
                        errors.append('MARKET_HTTP_BUDGET_EXHAUSTED')
                    continue
                error = None
                try:
                    response = self.client.call(t.asset.api_name, t.params, QUOTE_FIELDS)
                    result = repo.persist(t, response, captured_at=checked_at)
                    if result['status'] == 'completed':
                        completed.append(code)
                        retained += result['retainedCount']
                    else:
                        error = 'MARKET_RESPONSE_REJECTED'
                except TushareError as exc:
                    error = type(exc).__name__.upper()
                if error:
                    pending.append(code)
                    errors.append(error)
                    with self.factory.begin() as session:
                        session.add(JobFailure(job_run_id=run_id, dataset=JOB_NAME,
                            request_key=t.identity, error_code=error, message=error,
                            request_payload={'assetCode': code}, retryable=True,
                            last_attempted_at=checked_at))
            status = 'completed' if not pending else 'partial' if completed else 'failed'
            with self.factory.begin() as session:
                run = session.get(JobRun, run_id)
                run.status, run.request_count, run.row_count = status, requests, retained
                run.finished_at = checked_at
                run.details = {'completedAssets': completed, 'pendingAssets': pending, 'errorCodes': sorted(set(errors))}
            return MarketSyncReport(status, requests, retained, tuple(completed), tuple(pending), tuple(sorted(set(errors))))
        except Exception:
            if requests == 0:
                raise
            # 异常信息不包含原始数据库/认证内容；保留本进程已观察到的请求数。
            code = 'MARKET_SYNC_FAILED'
            remaining = tuple(t.asset.asset_code for t in targets if t.asset.asset_code not in completed)
            try:
                with self.factory.begin() as session:
                    run = session.get(JobRun,run_id)
                    if run is not None:
                        run.status,run.request_count,run.row_count = 'failed',requests,retained
                        run.finished_at = checked_at
                        run.details = {'errorCodes':[code],'pendingAssets':list(remaining)}
            except Exception:
                pass  # 数据库确实不可写时不捏造已保存；返回已观察到的安全回执。
            return MarketSyncReport('failed',requests,retained,tuple(completed),remaining,(code,))
        finally:
            self.lock.release(JOB_NAME)
