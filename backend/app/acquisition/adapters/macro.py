from contextlib import nullcontext
from datetime import datetime

from app.acquisition.contracts import Evidence, StateKey
from app.macro.audit import CollectionAuditReader
from app.macro.audit_records import EXCHANGE, OFFICIAL, extension_entries, financial_entries
from app.acquisition.tracking import record_terminal


def legacy_id(key):
    if key.entry_id.startswith('macro-extension-'):
        group = key.entry_id.removeprefix('macro-extension-')
        return 'collection:official:'+group if group in OFFICIAL else key.item_id
    if key.entry_id in ('stock-income','stock-balance','stock-cashflow'):
        return 'collection:financial:'+key.entry_id.removeprefix('stock-')+':'+key.subject_key
    return key.subject_key


def read_macro_evidence(session, keys, *, now):
    keys = [k for k in keys if k.scope_kind == 'routine']
    if not keys:
        return {}
    factory = lambda: nullcontext(session)
    reader = CollectionAuditReader(factory)
    rows = []
    if any(k.subject_kind == 'series' and not k.item_id for k in keys):
        rows.extend(reader.base_entries(now,reader.catalog_loader()))
    if any(k.entry_id.startswith('macro-extension-') for k in keys):
        rows.extend(extension_entries(factory,reader.extension_loader(),now))
    if any(k.entry_id.startswith('stock-') for k in keys):
        rows.extend(financial_entries(factory,reader.enterprise_enabled(),now,
                                      symbols=[k.subject_key for k in keys if k.entry_id.startswith('stock-')]))
    indexed = {row['id']:row for row in rows}
    result = {}
    for key in keys:
        sid = legacy_id(key)
        if sid not in indexed:
            continue
        row = indexed[sid]
        timestamp = lambda field: datetime.fromisoformat(row[field]) if row[field] else None
        result[key] = Evidence(row['status'], timestamp('checkedAt'),timestamp('successAt'),row['errorCode'],
                               '',{'seriesId':sid},({'collectionId':sid},))
    return result


def write_series_result(session, row, *, status, checked):
    code = row.last_error_code
    if code in ('MACRO_VALUE_EXPIRED','MACRO_HISTORY_GAP') or (code and code.endswith('_REQUEST_BUDGET_EXHAUSTED')):
        status = 'unrecorded'
    key = StateKey(row.series_id, '', 'series', row.series_id)
    scope = {'contract':row.catalog_version, 'seriesId':row.series_id}
    if status == 'success':
        scope['source'] = row.active_source
    record_terminal(session, key, started_at=checked, checked_at=checked, status=status, scope=scope,
                    code=code, references=({'seriesId':row.series_id},),
                    reason={'unrecorded':'质量或预算标记，未确认本轮获取结果','failed':'系列采集失败',
                            'success':'系列检查已完成'}[status])
