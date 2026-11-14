"""受限fields/items证据；没有HTTP、发布时刻推断或持久化。"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import math
import re

from app.macro.extensions.config import ExtensionError
from app.macro.extensions.plan import canonical, digest, validate_extension_plan


def number(value: object) -> Decimal:
    if type(value) not in (str, int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError()
    text = str(value)
    if len(text) > 100 or not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?', text):
        raise ValueError()
    result = Decimal(text)
    if not result.is_finite() or len(result.as_tuple().digits) > 60 or result.as_tuple().exponent < -30 or (result and not -30 <= result.adjusted() <= 30):
        raise ValueError()
    return result


def decimal_text(value: Decimal) -> str:
    # 不依赖进程全局Decimal context；保留输入精度，不转float。
    if not value:
        return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def quarter_date(label: object) -> date:
    if not isinstance(label, str) or not re.fullmatch(r'[0-9]{4}Q[1-4]', label):
        raise ValueError()
    return date(int(label[:4]), (int(label[-1]) - 1) * 3 + 1, 1)


def normalize_extension_response(plan: dict, response: object) -> dict:
    validate_extension_plan(plan)
    try:
        if type(response) is not dict or set(response) != {'fields', 'items'}:
            raise ValueError()
        fields, items = response['fields'], response['items']
        if type(fields) is not list or any(type(f) is not str for f in fields):
            raise ValueError()
        if len(fields) != len(set(fields)) or set(fields) != set(plan['query']['fields']):
            raise ValueError()
        if type(items) is not list or len(items) > plan['limits']['maxResponseRows']:
            raise ValueError()
        if len(canonical(response).encode('utf-8')) > plan['limits']['maxResponseBytes']:
            raise ValueError()
        observations, seen = [], set()
        for index, values in enumerate(items):
            if type(values) is not list or len(values) != len(fields):
                raise ValueError()
            row = dict(zip(fields, values, strict=True))
            missing = False
            if plan['groupId'] == 'usdcnh':
                if row['ts_code'] != 'USDCNH.FXCM' or row['exchange'] != 'FXCM':
                    raise ValueError()
                trade_date = row['trade_date']
                if type(trade_date) is not str or not re.fullmatch(r'[0-9]{8}', trade_date):
                    raise ValueError()
                period = date(int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:])).isoformat()
                bid, ask = number(row['bid_close']), number(row['ask_close'])
                if not 0 < bid <= ask:
                    raise ValueError()
                value = bid if plan['groupPolicy']['quoteSide'] == 'bid_close' else ask
                diagnostics = {'bidClose': decimal_text(bid), 'askClose': decimal_text(ask)}
            elif plan['groupId'] == 'vix':
                if type(row['date']) is not str or date.fromisoformat(row['date']).isoformat() != row['date']:
                    raise ValueError()
                period = row['date']
                if (row['realtime_start'] != plan['query']['params']['realtime_start']
                    or row['realtime_end'] != plan['query']['params']['realtime_end']
                    or type(row['value']) is not str):
                    raise ValueError()
                missing = row['value'] == '.'
                value = None if missing else number(row['value'])
                if value is not None and value <= 0:
                    raise ValueError()
                diagnostics = {'realtimeStart': row['realtime_start'], 'realtimeEnd': row['realtime_end']}
            else:
                period = row['quarter']
                quarter_date(period)
                value, real_yoy = number(row['gdp']), number(row['gdp_yoy'])
                if value <= 0:
                    raise ValueError()
                diagnostics = {'reportedRealYoy': decimal_text(real_yoy)}
            if not plan['startPeriod'] <= period <= plan['endPeriod'] or period in seen:
                raise ValueError()
            seen.add(period)
            if missing:
                continue
            observations.append({'period': period, 'value': decimal_text(value),
                'diagnostics': diagnostics,
                'sourceRef': {'row': index, 'planFingerprint': plan['fingerprint']}})
        result = {'schemaVersion': 1, 'plan': json.loads(canonical(plan)),
            'rawResponse': json.loads(canonical(response)),
            'status': 'available' if observations else 'unavailable',
            'releaseTimeBasis': 'not-provided',
            'observations': sorted(observations, key=lambda row: row['period'])}
        return {**result, 'evidenceFingerprint': digest(result)}
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError, RecursionError):
        raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None


def validate_evidence(evidence: dict) -> None:
    try:
        expected = normalize_extension_response(evidence['plan'], evidence['rawResponse'])
        if canonical(expected) != canonical(evidence):
            raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ExtensionError('EXTENSION_EVIDENCE_INVALID') from None
