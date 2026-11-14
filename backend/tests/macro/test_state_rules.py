from copy import deepcopy

import pytest

from app.macro import state_rules


IDS = {
    'credit': 'cn.credit.tsf_stock_yoy', 'money': 'cn.rate.dr007',
    'price': 'cn.ppi.yoy', 'm1': 'cn.money.m1_yoy', 'm2': 'cn.money.m2_yoy',
    'financing': 'cn.market.financing_ratio', 'bond': 'cn.rate.govbond_10y',
    'dividend': 'cn.market.dividend_yield',
}


def derive(values=None, *, units=None, stale=()):
    series = {IDS[key]: [{'month': '2026-09-01', 'value': value}]
              for key, value in (values or {}).items()}
    statuses = [{'id': identity, 'unit': (units or {}).get(key, '%'), 'stale': key in stale}
                for key, identity in IDS.items()]
    return state_rules.derive_macro_states(series, statuses)


@pytest.mark.parametrize('key,value,state', [
    ('credit', 10, 'expanding'), ('credit', 9.999, 'stable'), ('credit', 8, 'stable'),
    ('credit', 7.999, 'contracting'), ('money', 2, 'loose'), ('money', 2.6, 'neutral'),
    ('money', 2.601, 'tight'), ('price', 1, 'inflation'), ('price', 0, 'repair'),
    ('price', -0.0, 'repair'), ('price', -1, 'bottoming'), ('price', -1.001, 'deflation'),
])
def test_card_thresholds(key, value, state):
    assert derive({key: value})['cards'][key] == state


@pytest.mark.parametrize('spread,state', [(1, 'positive'), (.999, 'critical'),
                                         (-.999, 'critical'), (-1, 'negative')])
def test_scissors_thresholds(spread, state):
    assert derive({'m1': spread, 'm2': 0})['cards']['scissors'] == state


@pytest.mark.parametrize('values,state,score', [
    ({}, 'missing', None), ({'price': 0}, 'neutral', 50),
    ({'financing': 3.2}, 'hot', 75), ({'m1': 1, 'm2': 0, 'price': .1}, 'hot', 75),
    ({'m1': -1, 'm2': 0, 'price': -1}, 'cold', 25),
    ({'m1': -1, 'm2': 0, 'financing': 2}, 'cold', 25),
    ({'m1': -1, 'm2': 0, 'price': -1, 'financing': 3.2}, 'hot', 75),
    ({'m1': 0, 'm2': 0, 'price': -1}, 'neutral', 50),
])
def test_discrete_temperature_priority_and_missing(values, state, score):
    actual = derive(values)['thermometer']
    assert actual['state'] == state and actual['score'] == score


def test_unit_is_explicit_not_guessed_from_magnitude():
    assert derive({'financing': .032})['thermometer']['state'] == 'neutral'
    assert derive({'financing': .032}, units={'financing': 'ratio'})['thermometer']['state'] == 'hot'
    actual = derive({'money': 1.5, 'bond': 2.7}, units={'money': 'bp'})
    assert actual['cards']['money'] == 'tight'
    assert actual['signals']['money']['fallbackUsed'] is True
    assert actual['signals']['money']['inputIds'] == [IDS['bond']]
    assert derive({'price': 1}, units={'price': 'unknown'})['signals']['price']['reason'] == 'unit-unsupported'


def test_common_period_and_stale_are_not_disguised_as_latest():
    series = {IDS['m1']: [{'month': '2026-08-01', 'value': 3}, {'month': '2026-09-01', 'value': 4}],
              IDS['m2']: [{'month': '2026-08-01', 'value': 2}]}
    statuses = [{'id': IDS[key], 'unit': '%', 'stale': False} for key in ('m1', 'm2')]
    result = state_rules.derive_macro_states(series, statuses)['signals']['scissors']
    assert result['value'] == 1 and result['period'] == '2026-08-01'
    assert result['reason'] == 'latest-period-mismatch'
    assert result['inputLatestPeriods'][IDS['m1']] == '2026-09-01'
    series[IDS['m2']][0]['month'] = '2026-07-01'
    result = state_rules.derive_macro_states(series, statuses)['signals']['scissors']
    assert result['value'] is None and result['reason'] == 'no-common-period'
    assert derive({'price': 1}, stale=('price',))['signals']['price']['reason'] == 'input-stale'


@pytest.mark.parametrize('point', [
    {'month': '2026-02-30', 'value': 1}, {'month': '20260901', 'value': 1},
    {'month': '2026-09-01', 'value': True}, {'month': '2026-09-01', 'value': float('nan')},
    {'month': '2026-09-01', 'value': float('inf')}, {'month': '2026-09-01', 'value': '1'},
])
def test_invalid_points_fail_closed(point):
    with pytest.raises(ValueError):
        state_rules.derive_macro_states({IDS['price']: [point]}, [])


def test_duplicate_period_fails_closed():
    point = {'month': '2026-09-01', 'value': 1}
    with pytest.raises(ValueError):
        state_rules.derive_macro_states({IDS['price']: [point, deepcopy(point)]}, [])


def test_noncanonical_month_only_degrades_scissors_not_whole_snapshot():
    series = {IDS[key]: [{'month': '2026-09-02', 'value': 1}] for key in ('price', 'm1', 'm2')}
    statuses = [{'id': IDS[key], 'unit': '%', 'stale': False} for key in ('price', 'm1', 'm2')]
    actual = state_rules.derive_macro_states(series, statuses)
    assert actual['cards']['price'] == 'inflation'
    assert actual['signals']['scissors']['value'] is None
    assert actual['signals']['scissors']['reason'] == 'period-unsupported'
    assert actual['thermometer']['availableInputCount'] == 1
