"""受控手算与真实SQLite血缘；绝不连接供应商或业务库。"""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_plan import plan
from backend.tests.macro.test_extension_store import NOW, database, store


def saved(factory, tmp_path, group, response, *, completed=True):
    key = store(factory).reserve(plan(tmp_path, group), NOW)
    store(factory).receive(key, response, NOW + timedelta(seconds=1))
    if completed:
        store(factory).replay(key)
    return key


def load(factory, at=None):
    from app.macro.extensions.view import load_extension_inputs
    return load_extension_inputs(factory, at or NOW + timedelta(minutes=1))


def view(inputs, real=()):
    from app.macro.extensions.view import build_extension_view
    return build_extension_view(inputs, real)


def test_nominal_and_deflator_are_single_quarter_not_cumulative(database, tmp_path):
    _, factory = database
    response = gdp()
    response['items'][-1][-1] = '5'
    saved(factory, tmp_path, 'nominalGdp', response)
    result = view(load(factory), [{'month': '2026-04-01', 'value': 5}])
    point = next(p for p in result['nominalGdp'] if p['period'] == '2026Q2')
    assert point['amount'] == 140
    assert point['nominalYoy'] == pytest.approx(16.666666666666668)
    assert point['approxDeflator'] == pytest.approx(11.666666666666666)
    assert point['strictDeflator'] == pytest.approx(11.11111111111111)
    assert point['reason'] is None
    assert result['basis'] == 'retrieval-visible-restated-history-not-strict-pit'
    assert result['deflatorUnit'] == '百分点'
    assert 'rawResponse' not in str(result) and 'token' not in str(result)


@pytest.mark.parametrize('real,reason', [([], 'REAL_GDP_MISSING'),
    ([{'month': '2026-04-01', 'value': 4}], 'REAL_GDP_REVISION_MISMATCH'),
    ([{'month': '2026-01-01', 'value': 5.5}], 'REAL_GDP_MISSING')])
def test_unaligned_real_gdp_never_yields_a_fake_zero(database, tmp_path, real, reason):
    _, factory = database
    saved(factory, tmp_path, 'nominalGdp', gdp())
    point = view(load(factory), real)['nominalGdp'][-1]
    assert point['approxDeflator'] is None and point['strictDeflator'] is None
    assert point['reason'] == reason


def test_missing_previous_quarter_is_not_interpolated(database, tmp_path):
    _, factory = database
    response = gdp()
    response['items'] = response['items'][1:]
    saved(factory, tmp_path, 'nominalGdp', response)
    point = view(load(factory), [{'month': '2026-04-01', 'value': 5.5}])['nominalGdp'][-1]
    assert point['nominalYoy'] is None
    assert point['reason'] == 'MISSING_PREVIOUS_CUMULATIVE'


def test_noncanonical_real_gdp_period_is_not_rounded_to_a_quarter(database, tmp_path):
    _, factory = database
    saved(factory, tmp_path, 'nominalGdp', gdp())
    result = view(load(factory), [{'month': '2026-05-01', 'value': 5.5}])
    assert result['realGdp'] == []
    assert result['nominalGdp'][-1]['reason'] == 'REAL_GDP_MISSING'


def test_visibility_and_incomplete_raw_do_not_leak_future_data(database, tmp_path):
    _, factory = database
    key = saved(factory, tmp_path, 'usdcnh', fx(), completed=False)
    assert view(load(factory))['sources']['usdcnh']['points'] == []
    store(factory).replay(key)
    assert view(load(factory, NOW))['sources']['usdcnh']['points'] == []
    result = view(load(factory))
    source = result['sources']['usdcnh']
    assert source['id'] == 'fx.usdcnh' and source['unit'] == 'CNH/USD'
    assert source['quoteSide'] == 'bid_close'
    assert source['points'][0]['period'] == '2026-10-21'
    assert result['fx']['value'] == pytest.approx(7.123456789012345678)
    assert result['fx']['changePercent'] is None


def test_delayed_core_replay_does_not_rewrite_earlier_visibility(database, tmp_path):
    _, factory = database
    key = saved(factory, tmp_path, 'usdcnh', fx(), completed=False)
    cutoff = NOW + timedelta(seconds=2)
    assert view(load(factory, cutoff))['fx']['value'] is None
    store(factory).replay(key, clock=lambda: NOW + timedelta(minutes=10))
    assert view(load(factory, cutoff))['fx']['value'] is None
    assert view(load(factory, NOW + timedelta(minutes=11)))['fx']['value'] is not None


@pytest.mark.parametrize('target', ['raw', 'core-binding'])
def test_damaged_completed_lineage_fails_closed(database, tmp_path, target):
    _, factory = database
    key = saved(factory, tmp_path, 'usdcnh', fx())
    with factory.begin() as session:
        if target == 'raw':
            session.scalar(select(SourcePayload)).content_hash = '0' * 64
        else:
            row = session.get(JobRun, key)
            details = deepcopy(row.details)
            details['coreBindings'][0]['value'] = '9.0000000000'
            row.details = details
    with pytest.raises(ExtensionError):
        load(factory)


def test_public_contract_recomputes_and_rejects_derived_or_metadata_tampering(database, tmp_path):
    from app.macro.extensions.view import extension_view_valid
    _, factory = database
    saved(factory, tmp_path, 'nominalGdp', gdp())
    expected = view(load(factory), [{'month': '2026-04-01', 'value': 5.5}])
    assert extension_view_valid(expected)
    assert extension_view_valid(expected, expected)
    for mutation in ('value', 'unit', 'version', 'bool', 'date', 'extra'):
        changed = deepcopy(expected)
        if mutation == 'value': changed['nominalGdp'][-1]['approxDeflator'] = 999
        elif mutation == 'unit': changed['sources']['usdcnh']['unit'] = 'CNY/USD'
        elif mutation == 'version': changed['algorithmVersion'] = 'unknown'
        elif mutation == 'bool': changed['nominalGdp'][-1]['nominalYoy'] = True
        elif mutation == 'date': changed['sources']['nominalGdp']['retrievedAt'] = '20261022'
        else: changed['private'] = 'not allowed'
        assert not extension_view_valid(changed)
        assert not extension_view_valid(changed, expected)


def test_empty_state_and_failed_source_remain_named_not_neutral(database, tmp_path):
    _, factory = database
    result = view(load(factory))
    assert result['fx']['value'] is None and result['nominalGdp'] == []
    assert result['sources']['usdcnh']['reason'] == 'NOT_COLLECTED'
    p = plan(tmp_path)
    key = store(factory).reserve(p, NOW)
    store(factory).fail(key, 'EXTENSION_PERMISSION_ERROR', NOW + timedelta(seconds=1))
    result = view(load(factory))
    assert result['sources']['usdcnh']['reason'] == 'EXTENSION_PERMISSION_ERROR'
    assert result['sources']['usdcnh']['points'] == []
