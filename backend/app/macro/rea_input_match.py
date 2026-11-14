"""REA补充响应与旧Core的纯见证匹配；不采集、不写库。"""
from app.connectors.tushare.contracts import TushareResponse
from app.macro.enterprise import FinancialRecord, _number
from app.macro.rea_input_preview import META_FIELDS, REA_REPORT_FIELDS, rea_input_request, witness_fields
from app.macro.receivables_match import _date_value, _same_number


def match_rea_inputs(request: dict, source: FinancialRecord, response: TushareResponse, page_size: int) -> dict:
    if (not isinstance(request, dict) or not isinstance(request.get('poolVersion'), str)
            or request != rea_input_request(source, request['poolVersion'])):
        raise ValueError('ENTERPRISE_REA_SOURCE_BINDING_INVALID')
    if type(page_size) is not int or not 1 <= page_size <= 5000:
        raise ValueError('ENTERPRISE_REA_PAGE_SIZE_INVALID')
    if len(set(response.fields)) != len(response.fields):
        raise ValueError('ENTERPRISE_REA_RESPONSE_INVALID')
    rows = response.rows()
    def outcome(status):
        return {'status': status, 'fields': {}}
    if len(rows) >= page_size:
        return outcome('truncated')
    if not set(REA_REPORT_FIELDS[source.table]) <= set(response.fields):
        return outcome('response-witness-missing')
    metadata = {'ts_code': source.symbol, 'end_date': source.end_date.isoformat(),
                'ann_date': source.ann_date.isoformat() if source.ann_date else None,
                'f_ann_date': source.f_ann_date.isoformat() if source.f_ann_date else None,
                'report_type': source.report_type, 'comp_type': source.comp_type, 'update_flag': source.update_flag}
    def meta_equal(field, value):
        if field in ('ann_date', 'f_ann_date', 'end_date'):
            return _date_value(value) == metadata[field]
        return not isinstance(value, bool) and (str(value) if value is not None else None) == metadata[field]
    if any(not all(meta_equal(field, row[field]) for field in ('ts_code', 'end_date', 'report_type')) for row in rows):
        return outcome('identity-mismatch')
    matched = [row for row in rows if all(meta_equal(field, row[field]) for field in META_FIELDS)
               and all(_same_number(source.values[field], row[field]) for field in witness_fields(source.table))]
    if not matched:
        return outcome('no-match')
    if len(matched) != 1:
        return outcome('ambiguous')
    result = {}
    for field in request['missingFields']:
        if field not in response.fields:
            status, value = 'missing-field', None
        elif matched[0][field] is None:
            status, value = 'returned-null', None
        else:
            number = _number(matched[0][field])
            status, value = ('matched', str(number)) if number is not None and number >= 0 else ('invalid-value', None)
        result[field] = {'status': status, 'value': value}
    return {'status': 'matched', 'fields': result}
