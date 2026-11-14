"""VIX第三源；精确计划/缺值/地区，不借旧两组身份。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.core import MacroObservation
from app.db.models.raw import IngestionBatch
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.normalize import normalize_extension_response
from app.macro.extensions.plan import build_extension_plan
from app.macro.extensions.store import ExtensionStore
from backend.tests.macro.test_extension_plan import config, payload
from backend.tests.macro.test_extension_store import database, count

VIX_NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def vix_config(tmp_path):
    raw = payload()
    raw['groups']['vix'] = {**raw['groups']['usdcnh'], 'collectionEnabled': True,
        'lookbackPeriods': 180, 'quoteSide': None}
    return config(tmp_path, raw)


def vix_plan(tmp_path):
    return build_extension_plan('vix', vix_config(tmp_path), VIX_NOW)


def vix_rows():
    return {'fields': ['date', 'value', 'realtime_start', 'realtime_end'], 'items': [
        ['2026-10-21', '10.25', '2026-10-24', '2026-10-24'],
        ['2026-10-22', '.', '2026-10-24', '2026-10-24'],
        ['2026-10-23', '12.50', '2026-10-24', '2026-10-24']]}


def test_vix_plan_has_fixed_fred_identity_and_new_york_cutoff(tmp_path):
    p = vix_plan(tmp_path)
    assert p['source'] == 'fred' and p['seriesId'] == 'us.volatility.vix'
    assert (p['region'], p['unit'], p['frequency'], p['periodTimezone']) == ('US', 'index', 'daily', 'America/New_York')
    assert p['startPeriod'] == '2026-04-27' and p['endPeriod'] == '2026-10-23'
    assert p['query'] == {'api': 'fred/series/observations', 'fields': ['date', 'value', 'realtime_start', 'realtime_end'], 'params': {
        'series_id': 'VIXCLS', 'observation_start': '2026-04-27', 'observation_end': '2026-10-23',
        'realtime_start': '2026-10-24', 'realtime_end': '2026-10-24', 'units': 'lin', 'output_type': 1,
        'file_type': 'json', 'order_by': 'observation_date', 'sort_order': 'asc', 'offset': 0, 'limit': 366}}
    assert p['useScope'] == 'personal-local-noncommercial'
    assert 'api_key' not in p['query']['params']


def test_new_group_does_not_change_old_frozen_plan_fingerprints(tmp_path):
    c = vix_config(tmp_path)
    now = datetime.fromisoformat('2026-10-24T18:00:00+08:00')
    assert build_extension_plan('usdcnh', c, now)['fingerprint'] == 'db78e871d689b07c12ded998856c9dc9075f26e3c0bcb820824d640a8e22c2f5'
    assert build_extension_plan('nominalGdp', c, now)['fingerprint'] == '3cb50ae0547b29d7c0bf7f0d942b637ffc19eb39ccf47a1fcda689097f71c4c1'


def test_fred_dot_is_raw_missing_not_zero_and_precision_remains_decimal(tmp_path):
    raw = vix_rows()
    raw['items'][0][1] = '10.123456789012345678'
    evidence = normalize_extension_response(vix_plan(tmp_path), raw)
    assert [(p['period'], p['value'], p['sourceRef']['row']) for p in evidence['observations']] == [
        ('2026-10-21', '10.123456789012345678', 0), ('2026-10-23', '12.5', 2)]
    assert evidence['rawResponse']['items'][1][1] == '.'
    assert evidence['observations'][0]['diagnostics'] == {'realtimeStart': '2026-10-24', 'realtimeEnd': '2026-10-24'}


@pytest.mark.parametrize('column,value', [(0, '2026-10-24'), (0, '2026-02-30'), (0, '20261021'),
    (1, None), (1, True), (1, 12.5), (1, 'NaN'), (1, '0'), (1, '-1'), (2, '2026-10-25'), (3, '2026-10-23')])
def test_bad_vix_observation_is_rejected(tmp_path, column, value):
    p = vix_plan(tmp_path)
    raw = vix_rows()
    raw['items'][0][column] = value
    with pytest.raises(ExtensionError):
        normalize_extension_response(p, raw)


def test_duplicate_missing_dates_are_not_silently_accepted(tmp_path):
    p = vix_plan(tmp_path)
    raw = vix_rows()
    raw['items'].append(raw['items'][1])
    with pytest.raises(ExtensionError):
        normalize_extension_response(p, raw)


def test_vix_uses_us_core_and_replays_completed_raw_without_new_rows(database, tmp_path):
    _, factory = database
    p = vix_plan(tmp_path)
    store = ExtensionStore(factory)
    key = store.reserve(p, VIX_NOW)
    store.receive(key, vix_rows(), VIX_NOW + timedelta(seconds=1))
    assert store.replay(key, clock=lambda: VIX_NOW + timedelta(seconds=2))['insertedRows'] == 2
    assert store.replay(key)['insertedRows'] == 0
    assert store.inspect(p, VIX_NOW + timedelta(seconds=3))['status'] == 'completed'
    with factory() as session:
        first = session.get(MacroObservation, 1)
        assert first.region == 'US' and first.source == 'fred' and first.unit == 'index'
        assert session.get(IngestionBatch, key).source == 'fred'
    assert count(factory, MacroObservation) == 2


def test_vix_view_uses_completed_raw_and_preserves_retrieval_visibility(database, tmp_path):
    from app.macro.extensions.view import load_extension_inputs, build_extension_view, extension_view_valid
    _, factory = database
    p = vix_plan(tmp_path)
    store = ExtensionStore(factory)
    key = store.reserve(p, VIX_NOW)
    store.receive(key, vix_rows(), VIX_NOW + timedelta(seconds=1))
    store.replay(key, clock=lambda: VIX_NOW + timedelta(seconds=5))
    earlier = build_extension_view(load_extension_inputs(factory, VIX_NOW + timedelta(seconds=2)))
    assert earlier['vix']['value'] is None
    result = build_extension_view(load_extension_inputs(factory, VIX_NOW + timedelta(seconds=6)))
    assert result['schemaVersion'] == '1.1'
    assert result['sources']['vix']['unit'] == 'index'
    assert result['vix']['value'] == 12.5 and result['vix']['period'] == '2026-10-23'
    assert result['vix']['changePercent'] == pytest.approx(21.951219512195123)
    assert result['vix']['useScope'] == 'personal-local-noncommercial'
    assert 'FRED' in result['vix']['attribution'] and 'Chicago Board Options Exchange' in result['vix']['attribution']
    assert extension_view_valid(result)
    result['vix']['value'] = 13
    assert not extension_view_valid(result)


def test_legal_old_two_source_contract_is_still_valid_for_history():
    from app.macro.extensions.view import _empty, build_extension_view, extension_view_valid
    old = build_extension_view({'sources': {g: _empty(g) for g in ('usdcnh', 'nominalGdp')}})
    assert old['schemaVersion'] == '1.0' and 'vix' not in old
    assert extension_view_valid(old)
