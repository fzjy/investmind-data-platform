"""Versioned REA state rules over selected observations. No I/O or inferred units."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from math import isfinite

from app.macro.derivations import same_derived_contract


SIGNAL_INPUTS = {
    'credit': ('cn.credit.tsf_stock_yoy',),
    'money': ('cn.rate.dr007',),
    'price': ('cn.ppi.yoy',),
    'scissors': ('cn.money.m1_yoy', 'cn.money.m2_yoy'),
    'financing': ('cn.market.financing_ratio',),
    'stockBondSpread': ('cn.market.dividend_yield', 'cn.rate.govbond_10y'),
}
def _points(points: Sequence[Mapping[str, object]]) -> dict[str, Decimal]:
    result = {}
    for point in points:
        period, value = point.get('month'), point.get('value')
        if not isinstance(period, str) or type(value) not in (int, float) or not isfinite(value):
            raise ValueError('invalid state observation')
        parsed = date.fromisoformat(period)
        if parsed.isoformat() != period or period in result:
            raise ValueError('invalid or duplicate state period')
        result[period] = Decimal(str(value))
    return result


def derive_macro_states(
    series: Mapping[str, Sequence[Mapping[str, object]]],
    statuses: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Partial historical states are usable, never described as full/current coverage."""
    values = {key: _points(points) for key, points in series.items()}
    metadata = {}
    for item in statuses:
        identity = item.get('id')
        if not isinstance(identity, str) or identity in metadata:
            raise ValueError('invalid or duplicate state metadata')
        metadata[identity] = item

    def signal(ids: tuple[str, ...], *, fallback: bool = False) -> dict[str, object]:
        latest = {key: max(values.get(key, {}), default=None) for key in ids}
        stale = any(metadata.get(key, {}).get('stale') is True for key in ids)
        unit_bad = any(metadata.get(key, {}).get('unit') not in ('%', 'ratio') for key in ids)
        period_bad = ids == SIGNAL_INPUTS['scissors'] and any(
            not period.endswith('-01') for key in ids for period in values.get(key, {}))
        common = set(values.get(ids[0], {}))
        for key in ids[1:]:
            common &= values.get(key, {}).keys()
        period = max(common, default=None)
        reason = None
        if any(latest[key] is None for key in ids):
            reason = 'input-missing'
        elif unit_bad:
            reason = 'unit-unsupported'
        elif period_bad:
            reason = 'period-unsupported'
        elif period is None:
            reason = 'no-common-period'
        elif any(value != period for value in latest.values()):
            reason = 'latest-period-mismatch'
        elif stale:
            reason = 'input-stale'
        number = None
        if period is not None and not unit_bad and not period_bad:
            converted = [values[key][period] * (100 if metadata[key]['unit'] == 'ratio' else 1) for key in ids]
            number = float(converted[0] if len(ids) == 1 else converted[0] - converted[1])
            if not isfinite(number):
                raise ValueError('nonfinite macro state')
            if number == 0:
                number = 0.0
        else:
            period = None
        return {'value': number, 'period': period, 'unit': '%' if len(ids) == 1 else '百分点',
                'inputIds': list(ids), 'inputLatestPeriods': latest, 'stale': stale,
                'reason': reason, 'fallbackUsed': fallback}

    signals = {name: signal(ids) for name, ids in SIGNAL_INPUTS.items()}
    if signals['money']['value'] is None:
        fallback = signal(('cn.rate.govbond_10y',), fallback=True)
        if fallback['value'] is not None:
            signals['money'] = fallback
    credit, money, price, scissors = (signals[name]['value'] for name in ('credit', 'money', 'price', 'scissors'))
    financing = signals['financing']['value']
    cards = {
        'credit': 'missing' if credit is None else 'expanding' if credit >= 10 else 'stable' if credit >= 8 else 'contracting',
        'money': 'missing' if money is None else 'loose' if money <= 2 else 'neutral' if money <= 2.6 else 'tight',
        'price': 'missing' if price is None else 'inflation' if price >= 1 else 'repair' if price >= 0 else 'bottoming' if price >= -1 else 'deflation',
        'scissors': 'missing' if scissors is None else 'positive' if scissors >= 1 else 'critical' if scissors > -1 else 'negative',
    }
    count = sum(value is not None for value in (scissors, price, financing))
    state = 'neutral'
    if count == 0:
        state = 'missing'
    elif (financing is not None and financing >= 3.2) or (scissors is not None and price is not None and scissors > 0 and price > 0):
        state = 'hot'
    elif (scissors is not None and price is not None and scissors < 0 and price < 0) or (financing is not None and scissors is not None and financing <= 2 and scissors < 0):
        state = 'cold'
    return {'schemaVersion': '1.0', 'algorithmVersion': 'macro-state-v1', 'signals': signals,
            'cards': cards, 'thermometer': {'state': state, 'score': {'cold': 25, 'neutral': 50, 'hot': 75}.get(state),
                                        'availableInputCount': count}}


def macro_states_valid(payload: Mapping[str, object]) -> bool:
    """Legacy snapshots may omit the field; a present contract must match its inputs."""
    if 'stateInsights' not in payload:
        return True
    try:
        original = {item['id']: item['series'][0]['values'] for item in payload['trendDefinitions']}
        expected = derive_macro_states(original, payload['seriesStatus'])
        return same_derived_contract(payload['stateInsights'], expected)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False
