"""当前持久采集状态独立于发布快照；仅内存数据库，无供应商请求。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.models.ops import JobRun, MacroSeriesState
from app.db.session import create_engine_and_session
from app.macro.audit import CollectionAuditReader
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.routes import create_router
from app.shared.contracts import DataMeta, DataResult
from backend.tests.macro.test_extension_store import database

NOW = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
SID = 'cn.cpi.yoy'


@pytest.fixture
def setup(tmp_path):
    engine, factory = create_engine_and_session('sqlite+pysqlite:///'+str(tmp_path/'audit.db'))
    Base.metadata.create_all(engine)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    config = SimpleNamespace(groups={key: SimpleNamespace(collectionEnabled=True) for key in (
        'usdcnh', 'nominalGdp', 'vix', 'governmentBonds', 'householdCredit', 'tier1Housing',
        'specialBonds', 'propertyLoans', 'marginBalances', 'aShareFloatMv')})
    reader = CollectionAuditReader(factory, catalog_loader=lambda: catalog,
        extension_loader=lambda: config, enterprise_enabled=lambda: True, clock=lambda: NOW)
    yield factory, reader
    engine.dispose()


def entry(reader, sid=SID):
    report = reader()
    assert report['basis'] == 'current-collection-records'
    assert len({row['id'] for row in report['entries']}) == len(report['entries'])
    return next(row for row in report['entries'] if row['id'] == sid)


def state(factory, **changes):
    with factory.begin() as s:
        row = s.get(MacroSeriesState, SID)
        if row is None:
            row = MacroSeriesState(series_id=SID, catalog_version='test',
                last_checked_at=NOW-timedelta(days=1), last_success_at=NOW-timedelta(days=1),
                active_source='nbs', stale=False, consecutive_failures=0)
            s.add(row)
        for key, value in changes.items():
            setattr(row, key, value)


def run(factory, key, phase, *, group='usdcnh', ago=0, document=None, documents=None):
    started = NOW-timedelta(hours=ago+1)
    plan = {'groupId': group, 'scheduledAt': started.isoformat(), 'fingerprint': key,
        'documents': documents or []}
    job = 'macro-extension-v1' if group == 'usdcnh' else 'macro-official-housing-fiscal-v1:'+group
    with factory.begin() as s:
        s.add(JobRun(id=key, job_name=job, business_key=key, status='running' if phase == 'reserved' else phase,
            started_at=started, finished_at=None if phase == 'reserved' else started+timedelta(minutes=1),
            request_count=1, error_message='EXTENSION_TIMEOUT' if phase == 'failed' else None,
            details={'schemaVersion': 1, 'plan': plan, 'phase': phase, 'document': document}))


def test_no_record_pause_success_failure_recovery_and_no_date_expiry(setup):
    factory, reader = setup
    assert entry(reader)['status'] == 'unrecorded'
    assert entry(reader, 'cn.fx.usdcny')['status'] == 'paused'
    state(factory, last_checked_at=NOW-timedelta(days=300), last_success_at=NOW-timedelta(days=300))
    assert entry(reader)['status'] == 'success'
    state(factory, last_checked_at=NOW, last_error_code='MACRO_SOURCE_ERROR', stale=True)
    failed = entry(reader)
    assert failed['status'] == 'failed'
    assert failed['successAt'] != failed['checkedAt']
    state(factory, last_checked_at=NOW, last_success_at=NOW, last_error_code=None, stale=False)
    assert entry(reader)['status'] == 'success'


def test_legacy_freshness_unknown_and_inconsistent_states_never_success(setup):
    factory, reader = setup
    for code in ('MACRO_VALUE_EXPIRED', 'MACRO_HISTORY_GAP'):
        state(factory, last_checked_at=NOW, last_error_code=code, stale=True)
        assert entry(reader)['status'] == 'unrecorded'
    state(factory, last_checked_at=NOW, last_success_at=NOW-timedelta(days=1), last_error_code=None, stale=False)
    assert entry(reader)['status'] == 'unrecorded'


@pytest.mark.parametrize('code', [
    'US_RATES_REQUEST_BUDGET_EXHAUSTED', 'NBS_SYNC_REQUEST_BUDGET_EXHAUSTED',
    'TUSHARE_MACRO_REQUEST_BUDGET_EXHAUSTED', 'FRED_MACRO_REQUEST_BUDGET_EXHAUSTED',
    'MACRO_CATCHUP_REQUEST_BUDGET_EXHAUSTED',
])
def test_legacy_budget_records_are_not_collection_failures(setup, code):
    factory, reader = setup
    state(factory, last_checked_at=NOW, last_error_code=code, stale=True)
    row = entry(reader)
    assert row['status'] == 'unrecorded'
    assert row['errorCode'] == 'COLLECTION_LEGACY_NONEXECUTION_STATE'


def test_confirmed_collection_success_is_separate_from_history_quality_gap(setup):
    factory, reader = setup
    state(factory, last_checked_at=NOW, last_success_at=NOW, last_error_code=None,
        stale=True, stale_reason='MACRO_HISTORY_GAP')
    assert entry(reader)['status'] == 'success'


def test_extension_latest_attempt_outvotes_old_success_and_running_is_not_success(setup):
    factory, reader = setup
    run(factory, 'success', 'completed', ago=3)
    run(factory, 'failure', 'failed', ago=2)
    assert entry(reader, 'fx.usdcnh')['status'] == 'failed'
    run(factory, 'running', 'reserved', ago=1)
    assert entry(reader, 'fx.usdcnh')['status'] == 'running'
    run(factory, 'recovered', 'completed')
    assert entry(reader, 'fx.usdcnh')['status'] == 'success'


def test_partial_official_plan_never_becomes_group_success(setup):
    factory, reader = setup
    docs = [{'id': 'first', 'role': 'tier1-prices'}, {'id': 'report', 'role': 'tier1-prices'}]
    run(factory, 'partial', 'completed', group='tier1Housing', document=docs[0], documents=docs)
    row = entry(reader, 'collection:official:tier1Housing')
    assert row['status'] == 'unrecorded'
    assert row['errorCode'] == 'COLLECTION_PLAN_INCOMPLETE'


def test_route_keeps_published_data_and_meta_unchanged_and_reads_each_request(setup):
    factory, reader = setup
    published = DataResult(data={'strict': 'old'}, meta=DataMeta(source='database', version='v1', asOf='2026-11-01', stale=False))
    class Service:
        def get_insights(self, version=None):
            assert version == 'v1'
            return published
    app = FastAPI()
    app.include_router(create_router(service=Service(), collection_audit=reader))
    with TestClient(app) as client:
        state(factory)
        first = client.get('/api/data/macro-insights?version=v1').json()
        state(factory, last_checked_at=NOW, last_error_code='MACRO_SOURCE_ERROR')
        second = client.get('/api/data/macro-insights?version=v1').json()
    assert first['data'] == second['data'] == {'strict': 'old'}
    assert published.data == {'strict': 'old'}
    assert first['meta'] == second['meta'] == published.meta.model_dump(by_alias=True)
    assert next(r for r in first['collectionAudit']['entries'] if r['id'] == SID)['status'] == 'success'
    assert next(r for r in second['collectionAudit']['entries'] if r['id'] == SID)['status'] == 'failed'
    assert 'collectionAudit' not in first['data']
    assert 'auditPolicy' not in str(second)


def test_audit_failure_does_not_break_published_response():
    def broken():
        raise RuntimeError('private connection details')
    class Service:
        def get_insights(self, version=None):
            return DataResult(data={'old': True}, meta=DataMeta(source='database', version='v1', asOf='2026-11-01', stale=False))
    app = FastAPI()
    app.include_router(create_router(service=Service(), collection_audit=broken))
    with TestClient(app) as client:
        response = client.get('/api/data/macro-insights')
    assert response.status_code == 200
    assert response.json()['collectionAudit'] is None
    assert response.json()['data'] == {'old': True}
    assert response.json()['meta'] == {'source': 'database', 'version': 'v1', 'asOf': '2026-11-01', 'stale': False}
    assert 'private' not in response.text


def test_without_reader_returns_top_level_null_and_preserves_snapshot():
    published = DataResult(data={'strict': {'items': [1, 2]}}, meta=DataMeta(source='database', version='v1', asOf='2026-11-01', stale=False))
    class Service:
        def get_insights(self, version=None):
            return published
    app = FastAPI()
    app.include_router(create_router(service=Service()))
    with TestClient(app) as client:
        response = client.get('/api/data/macro-insights')
    assert response.json() == {**published.model_dump(by_alias=True), 'collectionAudit': None}
    assert response.headers['cache-control'] == 'no-store'


def test_running_core_scope_does_not_mark_other_series_running(setup):
    factory, reader = setup
    state(factory)
    with factory.begin() as s:
        s.add(JobRun(id='active', job_name='sync-macro', business_key='active', status='running',
            started_at=NOW, details={'collectionSeriesIds': [SID]}))
    assert entry(reader)['status'] == 'running'
    assert entry(reader, 'cn.ppi.yoy')['status'] == 'unrecorded'


def test_real_extension_success_noop_failure_and_recovery_records(database, tmp_path, monkeypatch):
    import json
    import httpx
    from app.macro.audit_records import extension_entries
    from backend.tests.macro.test_extension_runtime import enabled, execute
    from backend.tests.macro.test_extension_client import response
    from backend.tests.macro.test_extension_evidence import fx
    from backend.tests.macro.test_extension_store import NOW as started
    _, factory = database
    config = enabled(tmp_path, monkeypatch)
    transport = httpx.MockTransport(lambda _: response(200, content=json.dumps({'code': 0, 'data': fx()}).encode()))
    assert execute(factory, config, 'test-token', transport=transport, now=started)['status'] == 'completed'
    row = lambda: next(r for r in extension_entries(factory, config, started+timedelta(days=1)) if r['id'] == 'fx.usdcnh')
    before = row()
    assert before['status'] == 'success'
    assert execute(factory, config, '', transport=transport, now=started+timedelta(seconds=1))['supplierHttp'] == 0
    assert row() == before
    # 后续周期真实入口失败，旧成功时间仍保留。
    failed_at = started+timedelta(days=1)
    failed = execute(factory, config, 'test-token', transport=httpx.MockTransport(lambda _: response(500)), now=failed_at)
    assert failed['status'] == 'blocked'
    after = next(r for r in extension_entries(factory, config, failed_at) if r['id'] == 'fx.usdcnh')
    assert after['status'] == 'failed' and after['successAt'] == before['successAt']


def test_real_official_partial_complete_and_noop_are_group_scoped(database, monkeypatch):
    import httpx
    from app.macro.audit_records import extension_entries
    from backend.tests.macro.test_official_macro_runtime import setup as official_setup
    from backend.tests.macro.test_official_macro_plan import NOW as started
    from backend.tests.macro.test_official_household_credit import document
    from backend.tests.macro.test_extension_client import response
    _, factory = database
    module, config = official_setup(monkeypatch, cap=1)
    calls = []
    def handler(_):
        calls.append(1)
        return response(200, content=document(title='2026年前八个月金融统计数据报告') if len(calls) == 1 else document())
    def execute():
        return module.execute_official_macro_group(factory, config, 'householdCredit', clock=lambda: started, transport=httpx.MockTransport(handler))
    def row():
        return next(r for r in extension_entries(factory, config, started) if r['id'] == 'collection:official:householdCredit')
    assert execute()['status'] == 'partial'
    assert row()['status'] == 'unrecorded'
    assert execute()['status'] == 'completed'
    before = row()
    assert before['status'] == 'success'
    assert execute()['supplierHttp'] == 0 and row() == before


def test_real_exchange_bse_failure_does_not_contaminate_sh_or_sz(database, monkeypatch):
    from app.macro.audit_records import extension_entries
    from backend.tests.macro.test_exchange_summary_runtime import setup as exchange_setup, execute, handler_for
    from backend.tests.macro.test_exchange_summary_store import NOW as started
    _, factory = database
    config, module = exchange_setup(factory, monkeypatch)
    calls = []
    assert execute(factory, config, module, handler_for(factory, calls, fail_bse=True))['status'] == 'blocked'
    rows = {r['id']: r for r in extension_entries(factory, config, started)}
    assert rows['cn.market.margin_balance.sse']['status'] == 'success'
    assert rows['cn.market.margin_balance.szse']['status'] == 'success'
    assert rows['cn.market.margin_balance.bse']['status'] == 'failed'


def test_financial_records_are_per_symbol_and_kind_and_reader_is_readonly():
    from app.macro.audit_records import financial_entries
    from backend.tests.test_financial_supply import source, Client, module, db_state
    factory = source()
    try:
        client = Client()
        assert module().capture(factory, client, execute=True, max_http=1)['completedCount'] == 1
        before = db_state(factory)
        rows = financial_entries(factory, True, datetime.now(timezone.utc))
        assert db_state(factory) == before
        assert len(rows) == 1 and rows[0]['status'] == 'success'
        assert rows[0]['id'].startswith('collection:financial:income:')
        assert module().capture(factory, client, execute=True, max_http=2)['completedCount'] == 3
        rows = financial_entries(factory, True, datetime.now(timezone.utc))
        assert len(rows) == 3 and all(r['status'] == 'success' for r in rows)
        assert all(r['status'] == 'paused' for r in financial_entries(factory, False, datetime.now(timezone.utc)))
    finally:
        factory.kw['bind'].dispose()


def test_future_and_overlapping_timestamps_cannot_break_contract(setup):
    factory, reader = setup
    state(factory, last_checked_at=NOW+timedelta(days=1), last_success_at=NOW+timedelta(days=1))
    row = entry(reader)
    assert row['status'] == 'unrecorded'
    assert row['checkedAt'] is None and row['successAt'] is None
    run(factory, 'past', 'completed', ago=1)
    run(factory, 'future', 'completed', ago=-2)
    row = entry(reader, 'fx.usdcnh')
    assert row['status'] == 'unrecorded' and row['checkedAt'] is None
    assert row['successAt'] is None


@pytest.mark.parametrize('error_type', ['TimeoutError', None, ''])
def test_financial_completed_pages_failure_and_later_persisted_recovery(setup, error_type):
    """只验证读取持久状态变化；不新增或绕过生产失败根的恢复策略。"""
    import base64
    import json
    from app.db.models.ops import JobCursor
    from app.db.models.raw import IngestionBatch, SourcePayload
    from app.ingestion.repositories import content_hash
    from app.macro.audit_records import financial_entries
    factory, _ = setup
    job = 'financial-statement-evidence'
    def save(cycle, at, *, failed=False, only_first=False):
        contract = {'symbols': ['000001.SZ'], 'cycle': cycle}
        identity = content_hash(contract)
        with factory.begin() as s:
            s.add(JobCursor(job_name=job, cursor_key='contract:'+identity, cursor_value=contract))
            for offset in ([0] if failed or only_first else [0, 1]):
                bid = f'{cycle}-{offset}'
                phase = 'failed' if failed else 'completed'
                s.add(IngestionBatch(id=bid, source='tushare', dataset=job, mode='live', status=phase,
                    started_at=at-timedelta(seconds=1), finished_at=at, request_count=1, row_count=1, rejected_count=int(failed)))
                body = json.dumps({'data': {'has_more': offset == 0}}).encode()
                payload = {'_http': {'bodyBase64': base64.b64encode(body).decode()}}
                raw = SourcePayload(batch_id=bid, api_name='income', request_hash=identity, content_hash=content_hash(payload), payload=payload)
                s.add(raw)
                s.flush()
                s.add(JobCursor(job_name=job, cursor_key=f'{identity}:income:000001.SZ'+(f':offset:{offset}' if offset else ''),
                    cursor_value={'status': phase, 'batchId': bid, 'rawId': raw.id, 'responseContentHash': raw.content_hash,
                        'rowCount': 1, **({'errorType': error_type} if failed and error_type is not None else {})}))
    def row():
        return financial_entries(factory, True, NOW)[0]
    save(1, NOW-timedelta(hours=3))
    baseline = row()
    assert baseline['status'] == 'success'
    save(2, NOW-timedelta(hours=2), failed=True)
    failed = row()
    assert failed['status'] == 'failed' and failed['successAt'] == baseline['successAt']
    assert failed['errorCode'] == 'COLLECTION_FAILED'
    assert row() == failed  # 读取/未执行不覆写时间
    save(3, NOW-timedelta(hours=1), only_first=True)
    assert row()['status'] == 'unrecorded'  # 一行但 has_more=true，不能假装尾页
    save(4, NOW)
    recovered = row()
    assert recovered['status'] == 'success'
    assert recovered['successAt'] == recovered['checkedAt'] == NOW.isoformat()
    assert row() == recovered
    from app.macro.audit_records import record
    row = record('test', True, 'running', NOW, NOW+timedelta(seconds=1))
    assert row['successAt'] is None
