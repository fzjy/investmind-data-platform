"""把已持久化的完整计划映射到对象。单页成功不能代表整个计划。"""
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

from app.acquisition.adapters.macro import legacy_id
from app.acquisition.contracts import StateKey, utc
from app.acquisition.catalog import load_bindings
from app.acquisition.tracking import record_terminal
from app.macro.audit_records import extension_entries, financial_entries
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def write_summary(session,key,summary,scope,refs):
    if summary['status'] not in ('failed','success','unrecorded') or not summary['checkedAt']:
        return
    checked = datetime.fromisoformat(summary['checkedAt'])
    record_terminal(session,key,started_at=checked,checked_at=checked,status=summary['status'],scope=scope,
                    code=summary['errorCode'],reason='已核对该范围的采集计划',references=refs,
                    success_at=datetime.fromisoformat(summary['successAt']) if summary['successAt'] else None)


def record_extension(session, row, checked):
    from app.acquisition.adapters.aliases import record_source_document
    record_source_document(session,row,checked)
    plan = row.details['plan']
    group = plan['groupId']
    from app.macro.housing_history_plan import VERSIONS
    if plan.get('normalizerVersion') in VERSIONS:
        for binding in load_bindings(ROOT):
            if binding.entry_id != 'macro-extension-'+group:
                continue
            if row.details['phase'] == 'completed' and not any(
                b.get('seriesId')==binding.item_id for b in row.details.get('coreBindings',[])):
                continue
            key = StateKey(binding.entry_id,binding.item_id,'series',binding.item_id,'historical')
            record_terminal(session,key,started_at=utc(row.started_at),checked_at=checked,
                status='failed' if row.status=='failed' else 'success',
                code=row.error_message if row.status=='failed' else None,
                scope={'plan':plan['fingerprint'],'group':group,'document':row.details['document'].get('id')},
                references=({'runId':row.id},))
        return
    entry = 'macro-extension-'+group
    config = SimpleNamespace(groups={group:SimpleNamespace(collectionEnabled=True)})
    rows = {r['id']:r for r in extension_entries(lambda:nullcontext(session),config,checked)}
    for binding in load_bindings(ROOT):
        if binding.entry_id != entry:
            continue
        key = StateKey(entry,binding.item_id,'series',binding.item_id)
        summary = rows.get(legacy_id(key))
        if summary and summary['checkedAt'] and datetime.fromisoformat(summary['checkedAt']) == checked:
            write_summary(session,key,summary,{'group':group,'plan':row.details['plan'].get('fingerprint')},
                          ({'runId':row.id},))


def reference_unit(session, job, spec, identity, value, checked):
    api, params = spec['api'], spec['params']
    subject = params.get('ts_code') or params.get('symbol') or params.get('index_code')
    if not subject:
        return
    if job == 'financial-statement-evidence':
        for row in financial_entries(lambda:nullcontext(session),True,checked,symbols=(subject,)):
            kind = row['id'].split(':')[2]
            if kind != spec['target'].split(':')[0]:
                continue
            key = StateKey('stock-'+kind,'','security',subject)
            write_summary(session,key,row,{'source':'tushare','contract':identity},({'batchId':value['batchId']},))
        return
    if job == 'stock-business-evidence' and value['status'] == 'completed':
        return  # 主营业务在整个拆分窗口闭合后确认。
    entries = {'index_basic':'index-basic','index_weight':'index-weights','etf_basic':'etf-basic',
               'fina_mainbz':'stock-business'}
    if api not in entries:
        return
    key = StateKey(entries[api],'','security' if api=='fina_mainbz' else 'instrument',subject)
    scope = {'source':'tushare','contract':identity,'params':params,'api':api}
    if api=='fina_mainbz':
        scope['coverageContract'] = identity
    record_terminal(session,key,started_at=checked,checked_at=checked,status='success' if value['status']=='completed' else 'failed',
                    code='SOURCE_OR_VALIDATION_FAILED' if value['status']=='failed' else None,
                    scope=scope,references=({'batchId':value['batchId']},))
