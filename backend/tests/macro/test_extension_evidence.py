"""受控响应仅验证合同/手算，不是实际供应商验收。"""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.macro.extensions.config import ExtensionError
from app.macro.extensions.gdp import derive_nominal_gdp
from app.macro.extensions.normalize import normalize_extension_response
from backend.tests.macro.test_extension_plan import plan


def fx():
    return {'fields': ['ts_code', 'trade_date', 'exchange', 'bid_close', 'ask_close'],
        'items': [['USDCNH.FXCM', '20261021', 'FXCM', '7.123456789012345678', '7.13']]}


def gdp():
    return {'fields': ['quarter', 'gdp', 'gdp_yoy'], 'items': [
        ['2025Q1', '100', '4'], ['2025Q2', '220', '4.5'],
        ['2026Q1', '120', '5'], ['2026Q2', '260', '5.5']]}


def test_fx_precision_identity_and_reordered_columns(tmp_path):
    p, raw = plan(tmp_path), fx()
    result = normalize_extension_response(p, raw)
    assert result['status'] == 'available'
    observation = result['observations'][0]
    assert observation['value'] == '7.123456789012345678'
    assert observation['period'] == '2026-10-21'
    assert observation['sourceRef'] == {'row': 0, 'planFingerprint': p['fingerprint']}
    reverse = {'fields': raw['fields'][::-1], 'items': [raw['items'][0][::-1]]}
    assert normalize_extension_response(p, reverse)['observations'] == result['observations']
    assert result['releaseTimeBasis'] == 'not-provided'


@pytest.mark.parametrize('column,value', [(0, 'USDCNY.FXCM'), (1, '20261022'),
    (1, '20260230'), (2, 'OTHER'), (3, True), (3, 'NaN'), (3, float('inf')),
    (3, '0'), (3, '-1'), (4, '7.0'), (3, {'private': 'value'}), (3, '1e999999')])
def test_bad_fx_row_is_rejected_not_silently_filtered(tmp_path, column, value):
    raw = fx()
    raw['items'][0][column] = value
    with pytest.raises(ExtensionError, match='^EXTENSION_RESPONSE_INVALID$'):
        normalize_extension_response(plan(tmp_path), raw)


@pytest.mark.parametrize('case', ['duplicate-row', 'duplicate-field', 'wide', 'extra', 'missing', 'rows', 'bytes'])
def test_response_shape_and_resource_bounds(tmp_path, case):
    raw, p = fx(), plan(tmp_path)
    if case == 'duplicate-row': raw['items'] *= 2
    elif case == 'duplicate-field': raw['fields'][0] = 'trade_date'
    elif case == 'wide': raw['items'][0].append('private')
    elif case == 'extra': raw['private'] = 'do-not-echo'
    elif case == 'missing': raw.pop('fields')
    elif case == 'rows': raw['items'] *= 367
    else: raw['items'][0][0] = 'x' * 8388609
    with pytest.raises(ExtensionError):
        normalize_extension_response(p, raw)


def test_empty_is_unavailable_not_zero(tmp_path):
    raw = fx()
    raw['items'] = []
    result = normalize_extension_response(plan(tmp_path), raw)
    assert result['status'] == 'unavailable' and result['observations'] == []


def test_gdp_uses_single_quarter_not_ytd_yoy_and_keeps_diagnostics(tmp_path):
    result = normalize_extension_response(plan(tmp_path, 'nominalGdp'), gdp())
    derived = derive_nominal_gdp(result)
    q2 = next(p for p in derived['points'] if p['period'] == '2026Q2')
    assert q2['amount'] == '140'
    assert abs(Decimal(q2['nominalYoy']) - Decimal('16.66666666666666666666666667')) < Decimal('1e-24')
    assert q2['status'] == 'available' and len(q2['sourceRefs']) == 4
    assert result['observations'][-1]['diagnostics']['reportedRealYoy'] == '5.5'


def test_missing_prior_and_nonpositive_single_quarter_remain_missing(tmp_path):
    raw = gdp()
    del raw['items'][0]
    raw['items'][-1][1] = '119'
    evidence = normalize_extension_response(plan(tmp_path, 'nominalGdp'), raw)
    points = derive_nominal_gdp(evidence)['points']
    q2 = next(p for p in points if p['period'] == '2026Q2')
    assert q2['status'] == 'unavailable' and q2['nominalYoy'] is None
    assert q2['reason'] == 'NONPOSITIVE_SINGLE_QUARTER'


def test_evidence_and_rehashed_arbitrary_plan_tampering_is_rejected(tmp_path):
    from app.macro.extensions.plan import digest
    p = plan(tmp_path, 'nominalGdp')
    altered = deepcopy(p)
    altered['query']['api'] = 'secret'
    altered['fingerprint'] = digest({k: v for k, v in altered.items() if k != 'fingerprint'})
    with pytest.raises(ExtensionError): normalize_extension_response(altered, gdp())
    evidence = normalize_extension_response(p, gdp())
    evidence['observations'][0]['value'] = '999'
    with pytest.raises(ExtensionError): derive_nominal_gdp(evidence)


@pytest.mark.parametrize('quarter', ['2026Q4', '2026Q0', '26Q1', '2025Q5'])
def test_gdp_invalid_or_future_period(tmp_path, quarter):
    raw = gdp()
    raw['items'][0][0] = quarter
    with pytest.raises(ExtensionError): normalize_extension_response(plan(tmp_path, 'nominalGdp'), raw)


def test_missing_and_zero_comparison_quarter_have_no_yoy(tmp_path):
    raw = gdp()
    raw['items'][1][1] = '100'  # 上年Q2单季为0，不除0也不回退累计同比。
    value = derive_nominal_gdp(normalize_extension_response(plan(tmp_path, 'nominalGdp'), raw))
    q2 = next(row for row in value['points'] if row['period'] == '2026Q2')
    assert q2['amount'] == '140' and q2['nominalYoy'] is None
    assert q2['reason'] == 'NONPOSITIVE_SINGLE_QUARTER'
    raw['items'] = [['2026Q1', '120', '-0']]
    evidence = normalize_extension_response(plan(tmp_path, 'nominalGdp'), raw)
    assert evidence['observations'][0]['diagnostics']['reportedRealYoy'] == '0'
    assert derive_nominal_gdp(evidence)['points'][0]['reason'] == 'MISSING_SAME_QUARTER'
