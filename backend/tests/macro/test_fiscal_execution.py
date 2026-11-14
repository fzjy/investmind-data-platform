from datetime import timedelta
import importlib
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionConfig, ExtensionError
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_fiscal_plan import NOW, DOCUMENTS, fiscal_payload


def body(year):
    rows = ''.join(f'<tr><td>{year}.{month:02}</td><td>{-1 if month == 1 else 0 if month == 2 else month * 100}</td></tr>' for month in range(1, 13))
    return ('<meta charset="utf-8"><table><tr><td colspan="2">社会融资规模增量统计表</td></tr>'
        '<tr><td colspan="2">单位：亿元人民币</td></tr><tr><th>月份</th><th>政府债券</th></tr>' + rows + '</table>').encode()


def setup(tmp_path, monkeypatch, *, lookback=48, per_run=5):
    from app.macro.extensions import config as module
    raw = fiscal_payload(); raw['groups']['governmentBonds']['lookbackPeriods'] = lookback
    raw['groups']['governmentBonds']['limits']['maxHttpPerRun'] = per_run
    file = tmp_path / 'fiscal-config.json'; file.write_text(json.dumps(raw), encoding='utf-8')
    monkeypatch.setattr(module, 'DEFAULT_EXTENSION_CONFIG_FILE', file)
    return ExtensionConfig.model_validate(raw)


def execute(factory, config, **kwargs):
    module = importlib.import_module('app.macro.jobs.macro_fiscal_runtime')
    return module.execute_fiscal_group(factory, config, clock=lambda: NOW, **kwargs)


def store(factory):
    return importlib.import_module('app.macro.fiscal_store').FiscalStore(factory)


def test_five_reserved_gets_ingest_48_signed_months_and_reenter_without_http(database, tmp_path, monkeypatch):
    file, factory = database; c = setup(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET' and not request.url.query
        assert 'authorization' not in request.headers
        with factory() as session:
            jobs = session.scalars(select(JobRun)).all()
            assert len(jobs) == len(calls) and jobs[-1].request_count == 1
            assert any(row.details['phase'] == 'reserved' for row in jobs)
        year = next(d['year'] for d in DOCUMENTS if d['url'] == str(request.url))
        return response(200, content=body(year))
    first = execute(factory, c, transport=httpx.MockTransport(handler))
    assert first['status'] == 'completed' and first['supplierHttp'] == 5 and first['insertedRows'] == 48
    assert count(factory, SourcePayload) == 5 and count(factory, MacroObservation) == 48
    with factory() as session:
        rows = session.scalars(select(MacroObservation)).all()
        assert all(r.source == 'pboc' and r.unit == '亿元' and r.frequency == 'monthly' for r in rows)
        assert any(r.value == -1 for r in rows) and any(r.value == 0 for r in rows)
    before = file.read_bytes()
    again = execute(factory, c, transport=httpx.MockTransport(handler))
    assert again['status'] == 'completed' and again['supplierHttp'] == 0 and again['readOnly']
    assert len(calls) == 5 and file.read_bytes() == before


def test_uncertain_reservation_is_not_resent(database, tmp_path, monkeypatch):
    file, factory = database; c = setup(tmp_path, monkeypatch)
    from app.macro.extensions.plan import build_extension_plan
    p = build_extension_plan('governmentBonds', c, NOW)
    store(factory).reserve(p, p['documents'][0], NOW)
    before = file.read_bytes(); calls = []
    result = execute(factory, c, transport=httpx.MockTransport(lambda r: calls.append(r)))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 0 and calls == []
    assert file.read_bytes() == before


def test_received_document_replays_without_network(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    from app.macro.extensions.plan import build_extension_plan
    p = build_extension_plan('governmentBonds', c, NOW); s = store(factory)
    key = s.reserve(p, p['documents'][0], NOW)
    s.receive(key, body(2026), NOW)
    assert count(factory, SourcePayload) == 1 and count(factory, MacroObservation) == 0
    result = execute(factory, c, transport=httpx.MockTransport(lambda _: pytest.fail('must not send')))
    assert result['status'] == 'completed' and result['supplierHttp'] == 0 and result['insertedRows'] == 3


@pytest.mark.parametrize('status', [302, 401, 429, 503])
def test_failed_public_document_is_not_retried_or_refunded(database, tmp_path, monkeypatch, status):
    _, factory = database; c = setup(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        return response(status, headers={'location': 'https://evil.invalid'}, content=b'private response is not logged')
    a = execute(factory, c, transport=httpx.MockTransport(handler))
    b = execute(factory, c, transport=httpx.MockTransport(handler))
    assert a['status'] == b['status'] == 'blocked' and a['supplierHttp'] == 1 and b['supplierHttp'] == 0
    assert len(calls) == 1
    m = importlib.import_module('app.macro.jobs.macro_fiscal_runtime')
    later = m.execute_fiscal_group(factory, c, clock=lambda: NOW + timedelta(days=8), transport=httpx.MockTransport(handler))
    assert later['status'] == 'blocked' and later['supplierHttp'] == 0 and len(calls) == 1


def test_budget_resumes_two_two_one_across_processes(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, per_run=2)
    calls = []
    def handler(request):
        calls.append(request); return response(200, content=body(next(d['year'] for d in DOCUMENTS if d['url'] == str(request.url))))
    reports = [execute(factory, c, transport=httpx.MockTransport(handler)) for _ in range(4)]
    assert [r['supplierHttp'] for r in reports] == [2, 2, 1, 0]
    assert reports[-1]['status'] == 'completed' and len(calls) == 5


def test_invalid_table_raw_is_retained_and_not_retried(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request); return response(200, content=b'<html>not the statistics table</html>')
    first = execute(factory, c, transport=httpx.MockTransport(handler))
    again = execute(factory, c, transport=httpx.MockTransport(handler))
    assert first['status'] == again['status'] == 'blocked' and len(calls) == 1
    assert count(factory, SourcePayload) == 1 and count(factory, MacroObservation) == 0


def test_future_plan_cannot_reserve_before_real_due_point(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch)
    from app.macro.extensions.plan import build_extension_plan
    future = build_extension_plan('governmentBonds', c, NOW + timedelta(days=8))
    with pytest.raises(ExtensionError):
        store(factory).reserve(future, future['documents'][0], NOW)
    assert count(factory, JobRun) == count(factory, JobCursor) == 0


def test_orphan_root_is_invalid_instead_of_new_budget(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch)
    from app.macro.extensions.plan import build_extension_plan
    from app.macro.fiscal_store import JOB
    p = build_extension_plan('governmentBonds', c, NOW)
    with factory.begin() as session:
        session.add(JobCursor(job_name=JOB, cursor_key=p['scheduledAt'], cursor_value={'plan': p}))
    with pytest.raises(ExtensionError): store(factory).inspect(p, NOW)


def test_completed_raw_requires_matching_batch_completion(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    with factory.begin() as session:
        batch = session.scalars(select(IngestionBatch)).one(); batch.status = 'failed'
    result = execute(factory, c, transport=httpx.MockTransport(lambda _: pytest.fail('cannot send')))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 0


def test_core_tampering_blocks_completed_evidence_instead_of_resending(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    with factory.begin() as session:
        row = session.scalars(select(MacroObservation)).first(); row.value = 999
    result = execute(factory, c, transport=httpx.MockTransport(lambda _: pytest.fail('cannot send')))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 0


def test_collection_disabled_during_pre_send_causes_no_reservation(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch)
    def disable():
        p = tmp_path / 'fiscal-config.json'; raw = json.loads(p.read_text()); raw['groups']['governmentBonds']['collectionEnabled'] = False
        p.write_text(json.dumps(raw), encoding='utf-8')
    result = execute(factory, c, before_send=disable, transport=httpx.MockTransport(lambda _: pytest.fail('cannot send')))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 0 and count(factory, JobRun) == 0


def test_editing_schedule_inside_used_cycle_cannot_create_another_budget(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    raw = c.model_dump(); raw['groups']['governmentBonds']['schedule']['time'] = '10:00'
    changed = ExtensionConfig.model_validate(raw)
    from app.macro.extensions.plan import build_extension_plan
    with pytest.raises(ExtensionError, match='EXTENSION_POLICY_CHANGED'):
        store(factory).inspect(build_extension_plan('governmentBonds', changed, NOW), NOW)


def test_new_year_document_update_after_old_cycle_does_not_require_old_year_coverage(database, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    raw = c.model_dump(); docs = raw['groups']['governmentBonds']['documents']
    raw['groups']['governmentBonds']['documents'] = docs[1:] + [{'year': 2027,
        'url': 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2027/02/2027021400000000000.htm'}]
    c = ExtensionConfig.model_validate(raw)
    from app.macro.extensions.plan import build_extension_plan
    later = datetime(2027, 2, 8, 12, tzinfo=timezone.utc)
    state = store(factory).inspect(build_extension_plan('governmentBonds', c, later), later)
    assert state['status'] == 'new' and state['reservedCount'] == 0


def test_backdated_core_visibility_is_rejected(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    with factory.begin() as session:
        row = session.scalars(select(MacroObservation)).first(); row.released_at -= timedelta(days=100)
    result = execute(factory, c, transport=httpx.MockTransport(lambda _: pytest.fail('cannot send')))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 0


def test_parse_failure_commit_uncertainty_preserves_transport_receipt(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch)
    from app.macro.fiscal_store import FiscalStore
    monkeypatch.setattr(FiscalStore, 'fail', lambda *a: (_ for _ in ()).throw(RuntimeError('commit confirmation unknown')))
    result = execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=b'<html>invalid table</html>')))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 1 and not result['readOnly']
    assert count(factory, SourcePayload) == 1


def test_same_value_in_next_cycle_reuses_original_visibility_and_raw_origin(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    with factory() as session:
        old = {r.observed_at: (r.id, r.released_at) for r in session.scalars(select(MacroObservation))}
    from app.macro.jobs.macro_fiscal_runtime import execute_fiscal_group
    later = NOW + timedelta(days=8)
    result = execute_fiscal_group(factory, c, clock=lambda: later, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    assert result['status'] == 'completed' and result['supplierHttp'] == 1 and result['insertedRows'] == 1
    with factory() as session:
        for row in session.scalars(select(MacroObservation)):
            if row.observed_at in old:
                assert (row.id, row.released_at) == old[row.observed_at]
    again = execute_fiscal_group(factory, c, clock=lambda: later, transport=httpx.MockTransport(lambda _: pytest.fail('cannot send')))
    assert again['status'] == 'completed' and again['supplierHttp'] == 0


def test_final_read_failure_preserves_all_completed_request_counts(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    from app.macro.fiscal_store import FiscalStore
    original, checks = FiscalStore.inspect, []
    def inspect(self, *args):
        checks.append(1)
        if len(checks) == 3:
            raise RuntimeError('final read unavailable')
        return original(self, *args)
    monkeypatch.setattr(FiscalStore, 'inspect', inspect)
    result = execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 1 and result['responseCount'] == 1
    assert not result['readOnly'] and count(factory, MacroObservation) == 3
