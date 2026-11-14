from datetime import date, datetime, timezone, timedelta
import json
import httpx
from sqlalchemy import select, func

from app.connectors.tushare.client import TushareClient
from app.db.models import JobCursor, JobRun
from app.jobs.locks import ProcessLocalJobLock
from app.stocks.update.planning import StockUpdateOptions
from backend.tests.stock_update.test_planning import fixtures

NOW = datetime(2026, 10, 14, 0, tzinfo=timezone.utc)


def task(budget=512, override=None, market_ready=False):
    from app.stocks.update.sync import StockUpdateSync
    factory, pool = fixtures()
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if override:
            result = override(body, len(calls))
            if result is not None:
                return result
        fields, params = body['fields'].split(','), body['params']
        rows = []
        if body['api_name'] == 'trade_cal':
            day = date.fromisoformat(f"{params['start_date'][:4]}-{params['start_date'][4:6]}-{params['start_date'][6:]}")
            end = date.fromisoformat(f"{params['end_date'][:4]}-{params['end_date'][4:6]}-{params['end_date'][6:]}")
            while day <= end:
                rows.append({'exchange': params['exchange'], 'cal_date': day.strftime('%Y%m%d'), 'is_open': int(day.weekday()<5)})
                day += timedelta(days=1)
        elif market_ready and body['api_name'] in ('daily', 'daily_basic', 'adj_factor'):
            rows = [{'ts_code': params['ts_code'], 'trade_date': day, 'close': 10,
                'open': 10, 'high': 11, 'low': 9, 'pre_close': 10, 'pct_chg': 0, 'vol': 100, 'amount': 1000,
                'adj_factor': 1, 'total_mv': 100, 'circ_mv': 90, 'pe': 20, 'pb': 1} for day in ('20261012', '20261013')]
        return httpx.Response(200, json={'code': 0, 'data': {'fields': fields,
            'items': [[row.get(f) for f in fields] for row in rows]}})
    client = TushareClient('private-token', transport=httpx.MockTransport(handler), backoff_seconds=0)
    job = StockUpdateSync(factory, client, pool, StockUpdateOptions(max_requests=budget), ProcessLocalJobLock())
    return job, client, calls


def test_budget_partial_and_resume_keeps_frozen_units():
    from app.stocks.update.sync import StockUpdateSync
    job, client, calls = task(budget=3, market_ready=True)
    try:
        first = job.run(NOW)
        assert first['status'] == 'partial' and first['requests'] == 3
        assert first['asOf'] == '2026-10-13' and first['publishReady'] is False
        next_job = StockUpdateSync(job.factory, client, job.selection, StockUpdateOptions(), job.lock)
        second = next_job.run(NOW+timedelta(days=1))
        assert second['status'] == 'completed' and second['asOf'] == '2026-10-13'
        assert len(calls) == 252  # calendar2 + actions100 + market150, no financial evidence
        third = next_job.run(NOW)
        assert third['requests'] == 0
        assert third['publishReady'] is False and len(third['stocks']) == 50
    finally:
        client.close()


def test_physical_retries_share_single_budget():
    job, client, calls = task(budget=2, override=lambda body, count: httpx.Response(503))
    try:
        result = job.run(NOW)
        assert result['status'] == 'partial' and len(calls) == result['requests'] == 2
    finally:
        client.close()


def test_pre_report_field_checkpoint_resumes_before_adopting_new_fields(monkeypatch):
    from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
    job, client, calls = task(budget=1)
    try:
        with monkeypatch.context() as old:
            from app.connectors.tushare.strategies import LEGACY_CASHFLOW_FIELDS
            old.setitem(FIELD_REGISTRY, Dataset.CASHFLOW, LEGACY_CASHFLOW_FIELDS)
            old.setitem(FIELD_REGISTRY, Dataset.INCOME, tuple(f for f in FIELD_REGISTRY[Dataset.INCOME] if f != 'basic_eps'))
            old.setitem(FIELD_REGISTRY, Dataset.FINANCIAL_INDICATOR, tuple(
                f for f in FIELD_REGISTRY[Dataset.FINANCIAL_INDICATOR] if f not in {"q_eps", "q_roe", "eps", "bps"}))
            started = job.run(NOW)
        resumed = job.run(NOW)
        assert resumed["identity"] == started["identity"]
        assert resumed["status"] == "partial" and len(calls) == 2
    finally:
        client.close()


def test_invalid_calendar_stops_before_stock_http():
    def override(body, count):
        return httpx.Response(200, json={'code': 0, 'data': {'fields': body['fields'].split(','), 'items': []}})
    job, client, calls = task(override=override)
    try:
        result = job.run(NOW)
        assert result['status'] == 'failed' and result['publishReady'] is False
        assert len(calls) == 1 and calls[0]['api_name'] == 'trade_cal'
    finally:
        client.close()


def test_dry_preflight_has_no_cursor_or_run():
    job, client, calls = task()
    try:
        assert job.preflight(NOW)['calendarPending'] is True
        with job.factory() as s:
            assert s.scalar(select(func.count()).select_from(JobCursor)) == 0
            assert s.scalar(select(func.count()).select_from(JobRun)) == 0
        assert calls == []
    finally:
        client.close()
