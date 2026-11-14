"""将补充字段绑定到唯一旧Core见证；不访问供应商或数据库。"""
from datetime import date
from decimal import Decimal, InvalidOperation

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from app.macro.enterprise import FinancialRecord
from app.macro.receivables_preview import TARGET_FIELD, _core_binding
from app.macro.balance_supplement import RECEIVABLES, target_options


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _date_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return 'invalid-date'
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return 'invalid-date'


def _same_number(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    value = _number(left)
    return value is not None and value == _number(right)


def match_receivable(request: dict, source: FinancialRecord,
                     response: TushareResponse, page_size: int, *, target=RECEIVABLES) -> dict:
    target_options(target)
    field=target.field
    fields = [*FIELD_REGISTRY[Dataset.BALANCE_SHEET], field]
    params = {'ts_code': source.symbol, 'period': source.end_date.strftime('%Y%m%d'), 'report_type': '1'}
    if (source.table != 'balance' or request.get('coreBinding') != _core_binding(source)
            or request.get('apiName') != 'balancesheet' or request.get('params') != params
            or request.get('fields') != fields):
        raise ValueError('ENTERPRISE_SOURCE_BINDING_INVALID')
    if type(page_size) is not int or not 1 <= page_size <= 5000:
        raise ValueError('ENTERPRISE_PAGE_SIZE_INVALID')
    if len(set(response.fields)) != len(response.fields):
        raise ValueError('ENTERPRISE_RESPONSE_INVALID')
    try:
        rows = response.rows()
    except ValueError:
        raise ValueError('ENTERPRISE_RESPONSE_INVALID') from None
    metadata = {'ts_code': source.symbol, 'end_date': source.end_date.isoformat(),
        'ann_date': source.ann_date.isoformat() if source.ann_date else None,
        'f_ann_date': source.f_ann_date.isoformat() if source.f_ann_date else None,
        'report_type': source.report_type, 'comp_type': source.comp_type, 'update_flag': source.update_flag}
    numeric_fields = set(fields) - set(metadata) - {field}
    outcome = lambda status: {'status': status, 'value': None}
    if not numeric_fields <= set(source.values):
        return outcome('source-witness-missing')
    if len(rows) >= page_size:
        return outcome('truncated')
    if not set(FIELD_REGISTRY[Dataset.BALANCE_SHEET]) <= set(response.fields):
        return outcome('response-witness-missing')
    if field not in response.fields:
        return outcome('missing-field')
    def meta_equal(field, value):
        if field in ('ann_date', 'f_ann_date', 'end_date'):
            return _date_value(value) == metadata[field]
        return not isinstance(value, bool) and (str(value) if value is not None else None) == metadata[field]
    if any(not all(meta_equal(field, row[field]) for field in ('ts_code', 'end_date', 'report_type')) for row in rows):
        return outcome('identity-mismatch')
    matched = [row for row in rows
        if all(meta_equal(field, row[field]) for field in metadata)
        and all(_same_number(source.values[field], row[field]) for field in numeric_fields)]
    if not matched:
        return outcome('no-match')
    if len(matched) != 1:
        return outcome('ambiguous')
    value = matched[0][field]
    if value is None:
        return outcome('returned-null')
    number = _number(value)
    return {'status': 'matched', 'value': str(number)} if number is not None else outcome('invalid-value')
