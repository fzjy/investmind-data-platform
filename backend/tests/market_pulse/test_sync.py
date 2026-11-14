import importlib
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, func

from app.connectors.tushare.client import TushareClient
from app.db.models import DailyBasic, DailyPrice, Security, PublishedSnapshot, JobRun, SourcePayload
from app.jobs.locks import ProcessLocalJobLock
from app.market.data.catalog import MarketDataCatalog
from app.stocks.universes.models import UniverseSelection
from backend.tests.market_pulse.test_repository import DAY, NOW, CODE, _factory

OTHER = '920185.BJ'
POOL = UniverseSelection('representative-50', 'controlled', 'representative', (CODE, OTHER), 'controlled')


def setup(budget=1, handler=None):
    assert importlib.util.find_spec('app.market.pulse.sync') is not None, 'pulse sync missing'
    factory = _factory()
    with factory.begin() as session:
        for code in POOL.symbols:
            session.add(Security(ts_code=code, symbol=code[:6], name=code))
            session.add(DailyPrice(ts_code=code, trade_date=DAY, close=10, pct_chg=0, data={}))
            session.add(DailyBasic(ts_code=code, trade_date=DAY, data={'other': 'keep'}))
        session.add(PublishedSnapshot(product='screener', version='old', as_of=NOW, payload={}))
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if handler:
            return handler(body)
        params = body['params']
        return httpx.Response(200, json={'code': 0, 'data': {'fields': ['ts_code','trade_date','limit_status'],
            'items': [[params['ts_code'], params['trade_date'], 0 if params['ts_code'] == CODE else 6]]}})
    client = TushareClient('controlled', transport=httpx.MockTransport(transport), max_retries=0)
    catalog = MarketDataCatalog.load().model_copy(update={'pulse': MarketDataCatalog.load().pulse.model_copy(update={'max_requests_per_run': budget})})
    sync = importlib.import_module('app.market.pulse.sync').PulseSync(factory, client, POOL, catalog, ProcessLocalJobLock())
    return sync, factory, client, requests


def test_budget_resume_and_same_target_zero_http():
    sync, factory, client, requests = setup()
    with client:
        first = sync.run(NOW)
        assert first['status'] == 'partial' and first['requestCount'] == 1
        second = sync.run(NOW + timedelta(seconds=1))
        assert second['status'] == 'completed' and second['requestCount'] == 1
        assert len(requests) == 2
        assert requests[1]['params'] == {'ts_code': OTHER, 'trade_date': '20261016'}
        assert requests[1]['fields'] == 'ts_code,trade_date,limit_status'
        third = sync.run(NOW + timedelta(seconds=2))
        assert third['requestCount'] == 0 and third['status'] == 'completed'
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(SourcePayload)) == 2


def test_permission_stops_without_fake_completion():
    sync, factory, client, requests = setup(budget=60, handler=lambda _: httpx.Response(403))
    with client:
        report = sync.run(NOW)
    assert report['status'] == 'failed' and report['requestCount'] == 1 and len(requests) == 1
    assert len(report['pendingSymbols']) == 2 and not report['publishReady']
    with factory() as session:
        assert all('limit_status' not in row.data for row in session.scalars(select(DailyBasic)))


def test_dry_run_has_no_writes_or_http():
    sync, factory, client, requests = setup()
    with client:
        assert sync.preflight(NOW)['pendingCount'] == 2
    assert requests == []
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRun)) == 0


def test_stock_lock_blocks_http():
    sync, _, client, requests = setup()
    key = 'sync-stock-data:representative-50'
    assert sync.lock.try_acquire(key)
    try:
        with client:
            assert sync.run(NOW)['status'] == 'locked'
        assert requests == []
    finally:
        sync.lock.release(key)


def test_unexpected_persist_error_retains_actual_http_and_safe_run(monkeypatch):
    from app.market.pulse.repository import PulseRepository
    sync, factory, client, requests = setup()
    def fail(*args):
        raise RuntimeError('secret database password')
    monkeypatch.setattr(PulseRepository, 'persist', fail)
    with client:
        result = sync.run(NOW)
    assert result['status'] == 'failed' and result['requestCount'] == 1 and len(requests) == 1
    assert 'secret' not in str(result)
    with factory() as session:
        run = session.scalar(select(JobRun))
        assert run.status == 'failed' and run.request_count == 1


def test_target_change_blocks_publication_without_more_http(monkeypatch):
    sync, factory, client, _ = setup(budget=60)
    from app.market.pulse.repository import PulseRepository
    original = PulseRepository.target
    calls = 0
    def target(self, selection, checked_at):
        nonlocal calls
        calls += 1
        return original(self, selection, checked_at) if calls == 1 else DAY + timedelta(days=1)
    monkeypatch.setattr(PulseRepository, 'target', target)
    with client:
        result = sync.run(NOW, publish=True)
    assert result['status'] == 'partial'
    assert result['publication']['code'] == 'PULSE_TARGET_CHANGED'
    assert result['requestCount'] == 2


@pytest.mark.parametrize('failure', ['target_recheck', 'publisher'])
def test_publication_exception_preserves_collection_evidence(monkeypatch, failure):
    from app.market.pulse import sync as module
    from app.market.pulse.repository import PulseRepository
    sync, factory, client, requests = setup(budget=60)
    def fail(*args, **kwargs):
        raise RuntimeError('secret database credentials')
    if failure == 'publisher':
        monkeypatch.setattr(module, 'rebuild_market_marts', fail)
    else:
        original = PulseRepository.target
        calls = 0
        def target(self, *args):
            nonlocal calls
            calls += 1
            return original(self, *args) if calls == 1 else fail()
        monkeypatch.setattr(PulseRepository, 'target', target)
    with client:
        report = sync.run(NOW, publish=True)
    assert report['status'] == 'partial' and report['requestCount'] == 2 and len(requests) == 2
    assert set(report['completedSymbols']) == set(POOL.symbols) and report['pendingSymbols'] == []
    assert report['asOf'] == DAY.isoformat() and report['publishReady']
    assert report['publication']['code'] == 'PULSE_PUBLICATION_FAILED' and 'secret' not in str(report)
