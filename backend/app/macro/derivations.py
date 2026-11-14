"""Deterministic analytics of selected, visible macro observations; no I/O."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from math import isfinite


SPREADS = (
    ('cn.ppi_cpi_spread', 'PPI-CPI 剪刀差', 'cn.ppi.yoy', 'cn.cpi.yoy', 'monthly', '百分点', 1),
    ('cn.m2_m1_spread', 'M2-M1 剪刀差', 'cn.money.m2_yoy', 'cn.money.m1_yoy', 'monthly', '百分点', 1),
    ('cn.money.m1_minus_m2_yoy', 'M1-M2 差值', 'cn.money.m1_yoy', 'cn.money.m2_yoy', 'monthly', '百分点', 1),
    ('cn.govbond_dr007_spread', '国债-DR007 利差', 'cn.rate.govbond_10y', 'cn.rate.dr007', 'daily', '百分点', 1),
    ('us.treasury_fed_funds_spread', '美债-联邦基金利差', 'us.rate.treasury_10y', 'us.rate.effective_fed_funds', 'daily', '百分点', 1),
    ('cn.us.rate_spread', '中美利差', 'cn.rate.govbond_10y', 'us.rate.treasury_10y', 'daily', 'bp', 100),
)


def same_derived_contract(actual: object, expected: object) -> bool:
    """JSON equality with strict bool/number separation (Python's True equals 1)."""
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual == expected
    if isinstance(expected, (int, float)):
        return type(actual) in (int, float) and isfinite(actual) and actual == expected
    if isinstance(expected, Mapping):
        return (isinstance(actual, Mapping) and actual.keys() == expected.keys()
                and all(same_derived_contract(actual[key], value) for key, value in expected.items()))
    if isinstance(expected, (list, tuple)):
        return (isinstance(actual, (list, tuple)) and len(actual) == len(expected)
                and all(same_derived_contract(left, right) for left, right in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


def _values(points: Sequence[Mapping[str, object]]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for point in points:
        period, raw = point.get('month'), point.get('value')
        if not isinstance(period, str) or isinstance(raw, bool):
            raise ValueError('invalid macro point')
        date.fromisoformat(period)
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError('invalid macro value') from None
        if not value.is_finite() or period in values:
            raise ValueError('nonfinite or duplicate macro point')
        values[period] = value
    return values


def _month_number(period: str) -> int:
    parsed = date.fromisoformat(period)
    return parsed.year * 12 + parsed.month - 1


def _month_label(number: int) -> str:
    return f'{number // 12:04d}-{number % 12 + 1:02d}'


def _number(value: Decimal) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError('derived macro value is not finite')
    return number


def derive_macro_insights(
    series: Mapping[str, Sequence[Mapping[str, object]]],
    statuses: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Input is the canonical20 selected history, never a synthetic/forward-filled grid."""
    values = {key: _values(points) for key, points in series.items()}
    state = {item['id']: item for item in statuses}
    results = []
    for identity, title, first, second, frequency, unit, scale in SPREADS:
        left, right = values.get(first, {}), values.get(second, {})
        common = sorted(left.keys() & right.keys())
        latest = common[-1] if common else None
        input_latest = {first: max(left, default=None), second: max(right, default=None)}
        reason = None
        if not common:
            reason = 'no-common-period' if left and right else 'input-missing'
        elif any(period != latest for period in input_latest.values()):
            reason = 'latest-period-mismatch'
        elif any(state.get(key, {}).get('stale') for key in (first, second)):
            reason = 'input-stale'
        results.append({
            'id': identity, 'title': title, 'unit': unit, 'frequency': frequency,
            'inputIds': [first, second],
            'points': [{'month': period, 'value': _number((left[period] - right[period]) * scale)} for period in common],
            'latestPeriod': latest, 'inputLatestPeriods': input_latest,
            'status': 'missing' if not common else 'lagged' if reason else 'current', 'reason': reason,
        })

    compass_ids = ('cn.industry.value_added_yoy', 'cn.ppi.yoy')
    excluded = [{'seriesId': key, 'period': period, 'reason': 'NON_CANONICAL_MONTHLY_PERIOD'}
                for key in compass_ids for period in sorted(values.get(key, {})) if date.fromisoformat(period).day != 1]
    monthly = [{_month_number(period): value for period, value in values.get(key, {}).items()
                if date.fromisoformat(period).day == 1} for key in compass_ids]
    common_months = monthly[0].keys() & monthly[1].keys()
    end = max(common_months, default=None)
    compass_points, missing_periods, structural, unexpected = [], [], [], []
    if end is not None:
        for month in range(end - 11, end + 1):
            if all(key in common_months for key in range(month - 2, month + 1)):
                compass_points.append({'month': _month_label(month),
                    'iva': _number(sum(monthly[0][key] for key in range(month - 2, month + 1)) / 3),
                    'ppi': _number(sum(monthly[1][key] for key in range(month - 2, month + 1)) / 3)})
            else:
                missing_periods.append(_month_label(month))
                absent = [(index, key) for index, data in enumerate(monthly)
                          for key in range(month - 2, month + 1) if key not in data]
                # IVA has no independent Jan/Feb observation. Never split its combined release.
                destination = structural if all(index == 0 and key % 12 in (0, 1) for index, key in absent) else unexpected
                destination.append(_month_label(month))
    eligible = 12 - len(structural)
    latest_period = compass_points[-1]['month'] if compass_points else None
    latest_inputs = {key: max(values.get(key, {}), default=None) for key in compass_ids}
    lagged = bool(latest_period) and any(period is not None and period[:7] != latest_period for period in latest_inputs.values())
    compass = {
        'points': compass_points, 'inputIds': list(compass_ids), 'windowMonths': 3,
        'displayMonths': 12, 'expectedPoints': 12, 'eligiblePoints': eligible,
        'missingPeriods': missing_periods, 'structuralMissingPeriods': structural, 'unexpectedMissingPeriods': unexpected,
        'latestPeriod': latest_period,
        'status': 'complete' if len(compass_points) == eligible else 'partial' if compass_points else 'missing',
        'inputLatestPeriods': latest_inputs,
        'excludedInputPeriods': excluded,
        'stale': lagged or any(state.get(key, {}).get('stale') for key in compass_ids),
    }
    return {'schemaVersion': '1.0', 'algorithmVersion': 'macro-derived-v1', 'series': results, 'compass': compass}
