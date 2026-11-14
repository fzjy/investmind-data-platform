import importlib
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.connectors.tushare.client import TushareClient
from app.db.models import IngestionBatch, JobCursor
from app.jobs.locks import ProcessLocalJobLock
from backend.tests.market_data.test_catalog import catalog
from backend.tests.market_data.test_repository import NOW, setup


def job(factory, *, budget=20, handler=None):
    assert importlib.util.find_spec('app.market.data.sync') is not None, 'bounded market sync missing'
    value = catalog()
    payload = value.model_dump(mode='json', by_alias=True)
    payload['sync']['maxRequestsPerRun'] = budget
    value = type(value).model_validate(payload)
    requests = []
    def transport(request):
        params = json.loads(request.content)
        requests.append(params)
        if handler:
            return handler(params)
        return httpx.Response(200, json={'code': 0, 'data': {
            'fields': ['ts_code','trade_date','open','high','low','close','pre_close','pct_chg'],
            'items': [[params['params']['ts_code'], params['params']['end_date'], 100,100,100,100,100,0]]}})
    client = TushareClient('controlled-secret', transport=httpx.MockTransport(transport), max_retries=0)
    sync = importlib.import_module('app.market.data.sync').MarketDataSync(factory, client, value, ProcessLocalJobLock())
    return sync, client, requests


def test_completed_target_new_process_reentry_is_zero_http():
    _, factory, _ = setup()
    sync, client, requests = job(factory)
    try:
        report = sync.run(NOW)
        assert report.status == 'completed' and report.request_count == 15 and len(requests) == 15
        assert report.retained_count == 15 and len(report.completed_assets) == 15
    finally:
        client.close()
    sync, client, requests = job(factory)
    try:
        report = sync.run(NOW + timedelta(seconds=1))
        assert report.status == 'completed' and report.request_count == 0 and requests == []
        assert report.retained_count == 0
    finally:
        client.close()


def test_two_request_budget_stops_before_third_physical_http_and_resumes():
    _, factory, _ = setup()
    sync, client, requests = job(factory, budget=2)
    try:
        first = sync.run(NOW)
        assert first.status == 'partial' and first.request_count == 2 and len(requests) == 2
        assert len(first.pending_assets) == 13
        second = sync.run(NOW)
        assert second.request_count == 2 and len(second.completed_assets) == 4
        assert requests[2]['params']['ts_code'] == '399006.SZ'
    finally:
        client.close()


@pytest.mark.parametrize('status', [401,403,429,500])
def test_failed_asset_never_advances_success_cursor_or_retries(status):
    _, factory, _ = setup()
    sync, client, requests = job(factory, handler=lambda _: httpx.Response(status))
    try:
        report = sync.run(NOW)
        assert report.request_count == 15 and len(requests) == 15
        assert report.status == 'failed' and len(report.pending_assets) == 15
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(JobCursor)) == 0
        assert 'controlled-secret' not in str(report.to_payload())
    finally:
        client.close()


def test_dry_run_and_disabled_have_no_job_cursor_raw_or_http_side_effects():
    _, factory, _ = setup()
    sync, client, requests = job(factory)
    try:
        assert sync.preflight(NOW)['pendingCount'] == 15
        assert requests == []
        payload = sync.catalog.model_dump(mode='json', by_alias=True)
        payload['enabled'] = False
        sync.catalog = type(sync.catalog).model_validate(payload)
        assert sync.run(NOW).status == 'disabled' and requests == []
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(IngestionBatch)) == 0
            assert session.scalar(select(func.count()).select_from(JobCursor)) == 0
    finally:
        client.close()


def test_missing_core_is_repaired_without_refetching_other_assets():
    value, factory, _ = setup()
    sync, client, requests = job(factory)
    try:
        sync.run(NOW)
        with factory.begin() as session:
            session.query(value.MarketDailyQuote).filter_by(asset_code='000001.SH').delete()
        assert sync.run(NOW).request_count == 1
    finally:
        client.close()


def test_network_errors_count_once_without_implicit_retries():
    _, factory, _ = setup()
    def offline(_):
        raise httpx.ConnectError('controlled-secret')
    sync, client, requests = job(factory, handler=offline)
    try:
        report = sync.run(NOW + timedelta(days=1))
        assert report.request_count == 15 and len(requests) == 15 and report.status == 'failed'
        assert 'controlled-secret' not in str(report.to_payload())
    finally:
        client.close()


def test_forged_future_target_is_rejected_before_http():
    from dataclasses import replace
    from app.market.data.catalog import plan_market_targets
    _, factory, _ = setup()
    sync, client, requests = job(factory)
    targets = plan_market_targets(sync.catalog,NOW)
    sync.targets = (replace(targets[0],end_date=targets[0].end_date+timedelta(days=1)), *targets[1:])
    try:
        with pytest.raises(ValueError,match='MARKET_TARGET_INVALID'):
            sync.run(NOW)
        assert requests == []
    finally:
        client.close()


def test_unexpected_persist_failure_records_safe_failed_run_and_actual_http(monkeypatch):
    from app.db.models import JobRun
    from app.market.data.repository import MarketQuoteRepository
    _, factory, _ = setup()
    sync, client, requests = job(factory)
    def fail(*args,**kwargs):
        raise RuntimeError('secret database credentials')
    monkeypatch.setattr(MarketQuoteRepository,'persist',fail)
    try:
        report = sync.run(NOW)
        assert report.status == 'failed' and report.request_count == 1 and len(requests)==1
        with factory() as session:
            row = session.scalar(select(JobRun))
            assert row.status == 'failed' and row.request_count == 1
            assert 'secret' not in str(row.details)
    finally:
        client.close()
