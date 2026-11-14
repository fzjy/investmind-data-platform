from copy import deepcopy
from importlib import import_module

import pytest


def module():
    return import_module('app.macro.sz_market_calibration')


def data():
    return {'fields': ['trade_date', 'ts_code', 'count', 'float_mv'], 'items': [
        ['20261023', '主板A股', 1494, '20302648254733.43'],
        ['20261023', '创业板A股', 1412, '12848472603714.02'],
        ['20261023', '主板B股', 36, '35308435206.77'],
        ['20261023', '股票', 2942, '33186429293654.22'],
        ['20261023', 'ETF', 900, None], ['20261023', '不动产基金', 70, '123'],
    ]}


def test_exact_calibration_has_qualified_scope_and_does_not_rewrite_raw():
    raw = data(); old = deepcopy(raw)
    p = module().calibrate(raw, '2026-10-23')
    assert p['value'] == '331511.2085844745'
    assert p['amountYuan'] == '33151120858447.45' and p['count'] == 2906
    assert p['unitBasis'] == 'official-example-cross-calibrated'
    assert p['supplierUnitExplicit'] is False and p['independentSameDayWitness'] is False
    assert p['scope'] == '供应商报告的深市主板A股＋创业板A股'
    assert p['unit'] == '亿元' and p['period'] == '2026-10-23'
    assert raw == old


@pytest.mark.parametrize('kind', ['wrong-date', 'missing-a', 'duplicate', 'negative', 'null', 'nan',
    'count-bool', 'count-fraction', 'sum-amount', 'sum-count', 'old-sme', 'unknown-stock', 'missing-total'])
def test_invalid_business_structure_cannot_be_promoted(kind):
    raw = data()
    if kind == 'wrong-date': raw['items'][0][0] = '20261022'
    elif kind == 'missing-a': raw['items'].pop(1)
    elif kind == 'duplicate': raw['items'].append(deepcopy(raw['items'][0]))
    elif kind == 'negative': raw['items'][0][3] = '-1'
    elif kind == 'null': raw['items'][0][3] = None
    elif kind == 'nan': raw['items'][0][3] = 'NaN'
    elif kind == 'count-bool': raw['items'][0][2] = True
    elif kind == 'count-fraction': raw['items'][0][2] = '1494.1'
    elif kind == 'sum-amount': raw['items'][3][3] = '33186429293654.23'
    elif kind == 'sum-count': raw['items'][3][2] = 2943
    elif kind == 'old-sme': raw['items'].append(['20261023', '中小板', 10, '1'])
    elif kind == 'unknown-stock': raw['items'].append(['20261023', '新板A股', 10, '1'])
    elif kind == 'missing-total': raw['items'].pop(3)
    with pytest.raises(ValueError): module().calibrate(raw, '2026-10-23')


def test_cdr_is_checked_but_not_added_and_counts_are_not_fixed():
    raw = data(); raw['items'] = raw['items'][:4]
    raw['items'][0][2:4] = [2, '100000000.01']
    raw['items'][1][2:4] = [3, '200000000.02']
    raw['items'][2][2:4] = [4, '300000000.03']
    raw['items'][3][2:4] = [15, '1100000000.11']
    raw['items'].append(['20261023', '主板CDR', 6, '500000000.05'])
    result = module().calibrate(raw, '2026-10-23')
    assert result['count'] == 5 and result['value'] == '3.0000000003'


def test_reordered_fields_and_valid_zero_are_supported():
    raw = data()
    raw['fields'] = list(reversed(raw['fields']))
    raw['items'] = [list(reversed(row)) for row in raw['items']]
    assert module().calibrate(raw, '2026-10-23')['count'] == 2906
    raw = data(); raw['items'] = [[r[0], r[1], 0, '0'] for r in raw['items'][:4]]
    assert module().calibrate(raw, '2026-10-23')['value'] == '0'


def captured(factory):
    from datetime import timedelta
    from backend.tests.macro.test_sz_market_sample import parent, policy
    from backend.tests.macro.test_exchange_summary_store import NOW
    from app.macro.sz_market_sample import SzMarketSamplePolicy, build_sample_plan
    from app.macro.sz_market_sample_store import SzMarketSampleStore
    p = SzMarketSamplePolicy.model_validate(policy(parent(factory)))
    at = NOW + timedelta(minutes=1)
    plan = build_sample_plan(factory, p, at)
    raw = data()
    for row in raw['items']: row[0] = plan['targetDate'].replace('-', '')
    store = SzMarketSampleStore(factory); store.reserve(plan, at); store.finish(plan, at, data=raw)
    return at


from backend.tests.macro.test_extension_store import database, count


def test_dry_run_then_atomic_idempotent_core_without_http_or_raw_changes(database):
    from datetime import timedelta
    from app.db.models.ops import JobRun
    from app.db.models.core import MacroObservation
    from app.db.models.raw import SourcePayload
    from sqlalchemy import select
    _, f = database; at = captured(f) + timedelta(seconds=1)
    before = (count(f, JobRun), count(f, MacroObservation), count(f, SourcePayload))
    store = module().CalibrationStore(f)
    preview = store.replay(at, dry_run=True)
    assert preview['status'] == 'preview' and preview['supplierHttp'] == 0 and preview['readOnly'] is True
    assert (count(f, JobRun), count(f, MacroObservation), count(f, SourcePayload)) == before
    result = store.replay(at)
    assert result['insertedRows'] == 1 and result['supplierHttp'] == 0 and result['readOnly'] is False
    assert (count(f, JobRun), count(f, MacroObservation), count(f, SourcePayload)) == (before[0]+1, before[1]+1, before[2])
    proof = store.load(at)
    assert proof['point']['value'] == '331511.2085844745' and module().proof_valid(proof)
    again = store.replay(at + timedelta(days=5))
    assert again['readOnly'] is True and again['insertedRows'] == 0
    with f() as s:
        job = s.scalar(select(JobRun).where(JobRun.job_name == module().VERSION))
        assert job.request_count == 0 and job.row_count == 1
    assert store.load(at - timedelta(microseconds=1)) is None


@pytest.mark.parametrize('kind', ['core-value', 'core-unit', 'core-time', 'job-count', 'proof-hash', 'raw-flags', 'raw-data'])
def test_changed_bound_evidence_blocks_reentry(database, kind):
    from datetime import timedelta
    from app.db.models.ops import JobRun
    from app.db.models.core import MacroObservation
    from app.db.models.raw import SourcePayload
    from app.macro.sz_market_sample_store import RUN
    from sqlalchemy import select
    _, f = database; at = captured(f) + timedelta(seconds=1)
    store = module().CalibrationStore(f); store.replay(at)
    with f.begin() as s:
        job = s.scalar(select(JobRun).where(JobRun.job_name == module().VERSION))
        core = s.get(MacroObservation, job.details['proof']['coreBinding']['coreId'])
        if kind == 'core-value': core.value = 1
        elif kind == 'core-unit': core.unit = '元'
        elif kind == 'core-time': core.released_at = at + timedelta(seconds=1)
        elif kind == 'job-count': job.request_count = 1
        elif kind == 'proof-hash':
            d = deepcopy(job.details); d['proof']['evidenceHash'] = '0'*64; job.details = d
        else:
            sample = s.get(JobRun, RUN); raw = s.get(SourcePayload, sample.details['rawId'])
            d = deepcopy(raw.payload)
            if kind == 'raw-flags': d['coreEligible'] = True
            else: d['businessData']['items'][0][3] = '1'
            raw.payload = d
    with pytest.raises(ValueError): store.replay(at + timedelta(seconds=2))


def test_existing_unproved_core_and_write_failure_do_not_get_overwritten(database, monkeypatch):
    from datetime import timedelta
    from app.db.models.ops import JobRun
    from app.db.models.core import MacroObservation
    from sqlalchemy import event
    _, f = database; at = captured(f) + timedelta(seconds=1)
    before = count(f, JobRun)
    def fail(*_): raise RuntimeError('controlled insert failure')
    event.listen(MacroObservation, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError): module().CalibrationStore(f).replay(at)
    finally: event.remove(MacroObservation, 'before_insert', fail)
    assert count(f, JobRun) == before
    module().CalibrationStore(f).replay(at)
    with f.begin() as s:
        from sqlalchemy import delete
        s.execute(delete(JobRun).where(JobRun.job_name == module().VERSION))
    with pytest.raises(ValueError): module().CalibrationStore(f).replay(at)


def test_bound_calibration_view_uses_same_day_only_and_preserves_empty_receipt(database):
    from datetime import timedelta
    from app.macro.exchange_summary_view import load_exchange_summary_view, exchange_summary_view_valid
    from backend.tests.macro.test_exchange_summary_view import collect
    _, f = database; at = captured(f) + timedelta(seconds=1)
    module().CalibrationStore(f).replay(at)
    view = load_exchange_summary_view(f, at)
    assert view['schemaVersion'] == '1.1' and view['calibration']['point']['count'] == 2906
    assert view['monthly'][-1]['value'] is None
    collect(f, at=at)
    view = load_exchange_summary_view(f, at + timedelta(seconds=20))
    assert view['monthly'][-1]['tradeDate'] == '2026-10-22'
    assert view['monthly'][-1]['szFloatMvYiYuan'] == '331511.2085844745'
    assert view['monthly'][-1]['szUnitBasis'] == 'official-example-cross-calibrated'
    assert view['groups']['aShareFloatMv']['targets'][0]['documents'][1]['normalized']['status'] == 'empty'
    assert exchange_summary_view_valid(view)
    bad = deepcopy(view); bad['calibration']['point']['supplierUnitExplicit'] = True
    assert not exchange_summary_view_valid(bad)
    bad = deepcopy(view)
    c = bad['calibration']; c['sample']['retrievedAt'] = c['sample']['completedAt'] = '2026-10-22T01:00:00+00:00'
    c = module().build_proof(c['sample'], c['normalizedAt'], c['coreBinding']['coreId'])
    bad['calibration'] = c
    from app.macro.exchange_summary_view import evidence_hash
    bad['evidenceHash'] = evidence_hash(bad)
    assert not exchange_summary_view_valid(bad)


def test_replay_cli_has_no_network_date_or_force_options(monkeypatch):
    from typer.testing import CliRunner
    from app.cli.main import app
    import app.macro.jobs.sz_market_calibration_runtime as runtime
    monkeypatch.setattr(runtime, 'run_sz_market_calibration', lambda *a, **k:
        {'status': 'preview', 'supplierHttp': 0, 'readOnly': True})
    runner = CliRunner()
    assert runner.invoke(app, ['replay-sz-market-calibration', '--dry-run']).exit_code == 0
    for option in ('--force', '--date', '--source', '--max-http'):
        assert runner.invoke(app, ['replay-sz-market-calibration', option, 'x']).exit_code != 0


def test_waiter_refreshes_clock_after_lock_but_historical_load_keeps_cutoff(database):
    from datetime import timedelta
    _, factory = database; at = captured(factory) + timedelta(seconds=1)
    store = module().CalibrationStore(factory)
    store.replay(at + timedelta(microseconds=2))
    stale = at + timedelta(microseconds=1)
    result = store.replay(stale, clock=lambda: at + timedelta(microseconds=3))
    assert result['status'] == 'completed' and result['readOnly'] is True and result['insertedRows'] == 0
    assert store.load(stale) is None
