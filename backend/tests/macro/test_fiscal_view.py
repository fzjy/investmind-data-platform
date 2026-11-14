from copy import deepcopy
import importlib

import httpx
import pytest
from sqlalchemy import select

from app.db.models.ops import JobRun
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.plan import build_extension_plan
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_fiscal_execution import setup, execute, body
from backend.tests.macro.test_fiscal_plan import NOW, DOCUMENTS


def module():
    return importlib.import_module('app.macro.fiscal_view')


def controlled_fiscal_view(tmp_path):
    from backend.tests.macro.test_fiscal_plan import fiscal_config
    p = build_extension_plan('governmentBonds', fiscal_config(tmp_path), NOW)
    docs = [{'year': d['year'], 'url': d['url'], 'rawSha256': 'a' * 64, 'retrievedAt': NOW.isoformat(),
        'points': [{'period': f"{d['year']}-{m:02}-01", 'value': str(-1 if m == 1 else 0 if m == 2 else m * 100)} for m in range(1, 13)]}
        for d in DOCUMENTS]
    return module().build_fiscal_view(p, docs, {'scheduledAt': p['scheduledAt'], 'status': 'completed', 'errorCode': None})


def test_window_is_48_calendar_months_with_signed_values_and_recomputed_latest(tmp_path):
    v = controlled_fiscal_view(tmp_path)
    assert v['status'] == 'available' and len(v['points']) == 48 and not v['missingPeriods']
    assert v['points'][0] == {'month': '2022-10-01', 'value': 1000.0}
    assert v['latest'] == {'month': '2026-09-01', 'value': 900.0}
    assert v['points'][3]['value'] == -1 and v['points'][4]['value'] == 0
    assert module().fiscal_view_valid(v)
    for field, value in [('latest', None), ('status', 'partial'), ('missingPeriods', ['2022-10-01']), ('unit', '万亿元')]:
        bad = deepcopy(v); bad[field] = value
        assert not module().fiscal_view_valid(bad)


def test_missing_period_is_a_null_calendar_slot_instead_of_zero(tmp_path):
    v = controlled_fiscal_view(tmp_path)
    docs = deepcopy(v['documents']); docs[-1]['points'] = [p for p in docs[-1]['points'] if p['period'] != '2026-08-01']
    result = module().rebuild_fiscal_view(v['window'], docs, v['latestAttempt'])
    assert result['status'] == 'partial' and result['missingPeriods'] == ['2026-08-01']
    assert result['points'][-2] == {'month': '2026-08-01', 'value': None}


def test_only_completed_and_cutoff_visible_documents_become_fiscal_inputs(database, tmp_path, monkeypatch):
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    assert module().load_fiscal_inputs(factory, NOW) is None
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    result = module().load_fiscal_inputs(factory, NOW)
    assert result['status'] == 'available' and len(result['points']) == 3
    from datetime import timedelta
    assert module().load_fiscal_inputs(factory, NOW - timedelta(seconds=1)) is None
    with factory.begin() as session:
        row = session.scalars(select(JobRun)).one(); row.row_count += 1
    with pytest.raises(ExtensionError): module().load_fiscal_inputs(factory, NOW)


def test_fiscal_payload_is_bound_into_extension_quality_contract(tmp_path):
    from app.macro.extensions.view import _empty, build_extension_view, extension_view_valid
    fiscal = controlled_fiscal_view(tmp_path)
    v = build_extension_view({'sources': {g: _empty(g) for g in ('usdcnh', 'nominalGdp', 'vix')}, 'fiscal': fiscal})
    assert v['schemaVersion'] == '1.2' and v['fiscal']['latest']['value'] == 900
    assert extension_view_valid(v)
    v['fiscal']['points'][0]['value'] = 123
    assert not extension_view_valid(v)


def test_new_cycle_failure_keeps_previous_complete_window_visible(database, tmp_path, monkeypatch):
    from datetime import timedelta
    from app.macro.jobs.macro_fiscal_runtime import execute_fiscal_group
    _, factory = database; c = setup(tmp_path, monkeypatch, lookback=3)
    execute(factory, c, transport=httpx.MockTransport(lambda _: response(200, content=body(2026))))
    before = module().load_fiscal_inputs(factory, NOW)
    later = NOW + timedelta(days=8)
    failed = execute_fiscal_group(factory, c, clock=lambda: later, transport=httpx.MockTransport(lambda _: response(503)))
    assert failed['status'] == 'blocked'
    after = module().load_fiscal_inputs(factory, later)
    assert after['window'] == before['window'] and after['points'] == before['points']
    assert after['latestAttempt']['status'] == 'failed' and module().fiscal_view_valid(after)


def test_fiscal_source_and_window_must_match_independent_frozen_plan(tmp_path):
    v = controlled_fiscal_view(tmp_path)
    forged = deepcopy(v)
    replacement = 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/10/2026101400000000000.htm'
    forged['window']['expectedDocuments'][0]['url'] = replacement
    forged['documents'][0]['url'] = replacement
    forged['window']['planFingerprint'] = '0' * 64
    assert not module().fiscal_view_valid(forged)
    shifted = deepcopy(v)
    shifted['window']['startPeriod'] = '2022-01-01'; shifted['window']['endPeriod'] = '2025-12-01'
    with pytest.raises(ValueError):
        module().rebuild_fiscal_view(shifted['window'], shifted['documents'][:-1], shifted['latestAttempt'])
