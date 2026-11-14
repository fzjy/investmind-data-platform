"""北交所代码沿革：正式原件及同预算采集，不维护单证券名单。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import binascii
import re

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import SourcePayload
from app.ingestion.repositories import content_hash

FIELDS = ('o_code', 'n_code')
MAPPING_SYMBOL = 'bse_mapping'
ROW_CAP = 1000  # bse_mapping 文档375的单次供应商上限，满额不能证明未截断。


def validate_mapping_response(response, fields):
    if tuple(response.fields) != tuple(fields) or tuple(fields) != FIELDS or len(response.items) >= ROW_CAP:
        raise ValueError('MAPPING_FIELDS_OR_CAP_INVALID')
    old_codes = {}
    for row in response.rows():
        old, new = row.get('o_code'), row.get('n_code')
        if any(not isinstance(code, str) or not re.fullmatch(r'\d{6}\.BJ', code) for code in (old, new)) or old == new:
            raise ValueError('MAPPING_CODE_INVALID')
        if old in old_codes and old_codes[old] != new:
            raise ValueError('MAPPING_CONFLICT')
        old_codes[old] = new
    for code in old_codes:
        seen = set()
        while code in old_codes:
            if code in seen:
                raise ValueError('MAPPING_CYCLE')
            seen.add(code)
            code = old_codes[code]


def mapping_evidence(session, *, allow_pending=False):
    from app.portfolio.evidence import _get, _verify
    from app.portfolio.dividend_capture import _valid_saved
    from app.portfolio.dividend_ledger import _original_rows
    active = _get(session, 'mapping:active')
    if active is not None:
        _verify(active.cursor_value)
        if not allow_pending and active.cursor_value.get('status') == 'pending':
            identity = content_hash(active.cursor_value['target'])
            unit_row = _get(session, f'{identity}:unit:{MAPPING_SYMBOL}')
            status = unit_row.cursor_value.get('status', 'invalid') if unit_row is not None else 'pending'
            raise ValueError('mapping_query_' + status)
    saved = _get(session, 'mapping')
    if saved is None:
        raise ValueError('mapping_query_missing')
    value = saved.cursor_value
    _verify(value)
    unit = value['unit']
    if not _valid_saved(session, unit, MAPPING_SYMBOL, FIELDS, api='bse_mapping'):
        raise ValueError('mapping_source_invalid')
    raw = session.get(SourcePayload, unit['rawId'])
    rows = _original_rows(raw, list(FIELDS))
    validate_mapping_response(TushareResponse.model_validate(raw.payload), FIELDS)
    return value, rows


def security_aliases(session, symbols):
    result = {symbol: [symbol] for symbol in symbols}
    if not any(symbol.endswith('.BJ') for symbol in symbols):
        return result, None
    value, rows = mapping_evidence(session)
    edges = {row['o_code']: row['n_code'] for row in rows}
    def root(code):
        while code in edges:
            code = edges[code]
        return code
    for symbol in symbols:
        if symbol.endswith('.BJ'):
            result[symbol] = [symbol, *sorted(code for code in set(edges) | set(edges.values())
                if code != symbol and root(code) == root(symbol))]
    return result, value


def refresh_mapping(factory, client, *, base, now, execute, config, revision_days, completion_clock):
    from app.portfolio.evidence import JOB, _get, _save, _seal, _verify
    from app.portfolio.dividend_capture import capture_dividend_evidence
    with factory() as session:
        active_row = _get(session, 'mapping:active')
        active = deepcopy(active_row.cursor_value) if active_row is not None else None
        saved = _get(session, 'mapping')
        value = None
        if saved is not None:
            value, _ = mapping_evidence(session, allow_pending=True)
        dividend_active = _get(session, 'active')
        if active and active.get('status') == 'pending':
            target = active['target']
            _verify(active)
        elif value is not None and ((dividend_active is not None and dividend_active.cursor_value.get('status') == 'pending')
                or now - datetime.fromisoformat(value['checkedAt']) < timedelta(days=revision_days)):
            return dict(status='completed', requestCount=0, pendingCount=0)
        else:
            target = dict(baseVersion=base['baseVersion'], asOf=base['asOf'], symbols=[MAPPING_SYMBOL],
                apiName='bse_mapping', fields=list(FIELDS), cycleDate=now.astimezone(timezone.utc).date().isoformat())
    identity = content_hash(target)
    if execute:
        with factory.begin() as session:
            pointer = _seal(dict(status='pending', target=target))
            if active != pointer:
                _save(session, 'mapping:active', pointer)
    result = capture_dividend_evidence(factory, client, execute=execute, config=config, job=JOB,
        control_key='mapping:capture:' + identity, base_reader=lambda session: target, clock=completion_clock)
    if not execute or result['status'] != 'completed':
        return result
    try:
        with factory.begin() as session:
            unit = deepcopy(_get(session, f'{identity}:unit:{MAPPING_SYMBOL}').cursor_value)
            raw = session.get(SourcePayload, unit['rawId'])
            captured = raw.captured_at
            captured = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured.astimezone(timezone.utc)
            value = _seal(dict(unit=unit, asOf=target['asOf'], checkedAt=unit.get('completedAt', captured.isoformat())))
            _save(session, 'mapping', value)
            mapping_evidence(session, allow_pending=True)
            _save(session, 'mapping:active', _seal(dict(status='completed', target=target)))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, binascii.Error):
        return result | dict(status='blocked', code='DIVIDEND_MAPPING_EVIDENCE_INVALID', pendingCount=1)
    return result
