"""当前采集记录摘要；只读既有账本，不重新规范化或访问供应商。"""
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
import base64
import json
import re
from sqlalchemy import or_, select
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload

EXTENSIONS = {'usdcnh': 'fx.usdcnh', 'nominalGdp': 'cn.gdp.nominal_cumulative', 'vix': 'us.volatility.vix'}
OFFICIAL = ('householdCredit', 'tier1Housing', 'specialBonds', 'propertyLoans')
EXCHANGE = {
    'marginBalances': ('cn.market.margin_balance.sse', 'cn.market.margin_balance.szse', 'cn.market.margin_balance.bse'),
    'aShareFloatMv': ('cn.market.a_share_float_mv.sh', 'cn.market.a_share_float_mv.sz'),
}


def utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def record(sid, enabled, status='unrecorded', checked=None, success=None, error=None, *, now=None):
    checked, success = utc(checked), utc(success)
    if (error == 'COLLECTION_RECORD_INVALID' or (checked and now and checked > now)
        or (success and now and success > now) or (success and (checked is None or success > checked))):
        status, checked, success, error = 'unrecorded', None, None, 'COLLECTION_RECORD_INVALID'
    code = error if isinstance(error, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', error) else ('COLLECTION_FAILED' if error or status == 'failed' else None)
    return {'id': sid, 'enabled': enabled, 'status': status if enabled else 'paused',
        'checkedAt': utc(checked).isoformat() if checked else None,
        'successAt': utc(success).isoformat() if success else None, 'errorCode': code}


def lifecycle(row, now):
    start, end = utc(row.started_at), utc(row.finished_at)
    details = row.details if isinstance(row.details, dict) else {}
    phase = details.get('phase')
    if not start or start > now or (end and (end < start or end > now)):
        return 'unrecorded', None, 'COLLECTION_RECORD_INVALID'
    if row.request_count != 1 or details.get('schemaVersion') != 1:
        return 'unrecorded', end or start, 'COLLECTION_RECORD_INVALID'
    if phase in ('reserved', 'received') and row.status == 'running' and end is None:
        return 'running', start, None
    if phase == 'completed' and row.status == 'completed' and end and row.error_message is None:
        return 'success', end, None
    if phase == 'failed' and row.status == 'failed' and end:
        return 'failed', end, row.error_message or 'COLLECTION_FAILED'
    return 'unrecorded', end or start, 'COLLECTION_RECORD_INVALID'


def summarize(sid, enabled, rows, now):
    if not rows:
        return record(sid, enabled)
    assessed = [(r, *lifecycle(r, now)) for r in rows]
    latest = max(assessed, key=lambda item: utc(item[0].started_at))
    successful = [checked for _, status, checked, _ in assessed if status == 'success']
    return record(sid, enabled, latest[1], latest[2], max(successful, default=None), latest[3], now=now)


def plan_summary(sid, enabled, rows, now, *, directory_check=False):
    if not rows:
        return record(sid, enabled)
    plans = defaultdict(list)
    for row in rows:
        plans[row.details.get('plan', {}).get('fingerprint', '')].append(row)
    results = []
    for members in plans.values():
        # 恢复只覆盖同一文档槽，不覆盖其他文档的失败。
        slots = {}
        for row in sorted(members, key=lambda r: utc(r.started_at)):
            doc = row.details.get('document') or {}
            slots[str(doc.get('id', doc.get('year', '')))] = row
        plan = members[-1].details.get('plan', {})
        expected = {str(d.get('id', d.get('year', ''))) for d in plan.get('documents', [])}
        assessed = [lifecycle(r, now) for r in slots.values()]
        checked = max((v[1] for v in assessed if v[1]), default=None)
        failed = next((v for v in assessed if v[0] == 'failed'), None)
        invalid = next((v for v in assessed if v[0] == 'unrecorded'), None)
        complete = bool(expected) and set(slots) == expected and all(v[0] == 'success' for v in assessed)
        if directory_check and not complete and all(v[0] == 'success' for v in assessed):
            from app.macro.official_macro_store import checked_without_candidate
            complete = checked_without_candidate(plan, list(slots.values()))
        status, error = ('failed', failed[2]) if failed else ('unrecorded', invalid[2]) if invalid else (
            ('running', None) if any(v[0] == 'running' for v in assessed) else
            ('success', None) if complete else ('unrecorded', 'COLLECTION_PLAN_INCOMPLETE'))
        results.append((max(utc(r.started_at) for r in members), status, checked, error))
    latest = max(results, key=lambda r: r[0])
    return record(sid, enabled, latest[1], latest[2], max((r[2] for r in results if r[1] == 'success'), default=None), latest[3], now=now)


def extension_entries(factory, config, now):
    with factory() as session:
        rows = session.scalars(select(JobRun).where(or_(
            JobRun.job_name.in_(('macro-extension-v1', 'macro-extension-recovery-v1', 'macro-official-government-v1')),
            JobRun.job_name.startswith('macro-official-housing-fiscal-v1:'),
            JobRun.job_name.startswith('macro-official-access-replay-v1:'),
            JobRun.job_name.startswith('macro-exchange-summary-v1:')))).all()
        # 财政的离线规范化恢复不改失败原任务，完成凭据存在独立游标中。
        from app.macro.extensions.plan import digest
        receipts = {c.cursor_key: c for c in session.scalars(select(JobCursor).where(
            JobCursor.job_name == 'macro-official-government-raw-replay-v1'))}
        for index, row in enumerate(rows):
            cursor = receipts.get(row.id)
            if row.job_name != 'macro-official-government-v1' or cursor is None:
                continue
            value = cursor.cursor_value
            try:
                finished = utc(datetime.fromisoformat(value['completedAt']))
                valid = (row.status == 'failed' and row.error_message == 'FISCAL_TABLE_INVALID'
                    and value['sourceRunId'] == row.id and value['rawId'] == row.details['rawId']
                    and value['planFingerprint'] == row.details['plan']['fingerprint']
                    and value['normalizerVersion'] == 'official-government-table-v2'
                    and digest({k: v for k, v in value.items() if k != 'receiptHash'}) == value['receiptHash']
                    and utc(row.finished_at) <= finished <= now and finished == utc(cursor.updated_at))
            except (KeyError, TypeError, ValueError):
                valid = False
            rows[index] = SimpleNamespace(id=row.id, job_name=row.job_name, started_at=row.started_at,
                finished_at=finished if valid else row.finished_at, request_count=row.request_count,
                status='completed' if valid else 'unknown', error_message=None,
                details={**row.details, 'phase': 'completed' if valid else 'unknown'})
    grouped = defaultdict(list)
    for row in rows:
        if isinstance(row.details, dict) and isinstance(row.details.get('plan'), dict):
            grouped[row.details['plan'].get('groupId')].append(row)
        else:
            raise ValueError('COLLECTION_RECORD_INVALID')
    def enabled(key):
        return bool(config.groups.get(key) and config.groups[key].collectionEnabled)
    entries = [summarize(sid, enabled(key), grouped[key], now) for key, sid in EXTENSIONS.items()]
    entries.append(plan_summary('cn.fiscal.government_bond_net_financing', enabled('governmentBonds'), grouped['governmentBonds'], now))
    for group in OFFICIAL:
        # 历史补采有独立范围，不能用旧月补采冒充当前组检查恢复。
        entries.append(plan_summary('collection:official:'+group, enabled(group), grouped[group], now, directory_check=True))
    for group, ids in EXCHANGE.items():
        for sid in ids:
            matched = [r for r in grouped[group] if (r.details.get('document') or {}).get('seriesId') == sid]
            entries.append(summarize(sid, enabled(group), matched, now))
    return entries


def financial_entries(factory, enabled, now, *, symbols=()):
    """逐证券/报表汇总现有分页游标；不把整池成功映射为子范围成功。"""
    from app.ingestion.repositories import content_hash
    with factory() as session:
        query = select(JobCursor).where(JobCursor.job_name == 'financial-statement-evidence')
        if symbols:
            query = query.where(or_(*(condition for code in set(symbols) for condition in (
                JobCursor.cursor_key.endswith(':'+code),JobCursor.cursor_key.contains(':'+code+':offset:')))))
        cursors = session.scalars(query).all()
        if symbols and cursors:
            contracts_query = select(JobCursor).where(JobCursor.job_name == 'financial-statement-evidence',
                JobCursor.cursor_key.in_({'contract:'+r.cursor_key.partition(':')[0] for r in cursors}))
            cursors.extend(session.scalars(contracts_query))
        contracts = {r.cursor_key[9:]: r.cursor_value for r in cursors if r.cursor_key.startswith('contract:')}
        batch_ids = {r.cursor_value.get('batchId') for r in cursors if isinstance(r.cursor_value,dict)}
        batches = {b.id:b for b in session.scalars(select(IngestionBatch).where(
            IngestionBatch.dataset == 'financial-statement-evidence',IngestionBatch.id.in_(batch_ids)))}
        groups = defaultdict(lambda: defaultdict(list))
        for row in cursors:
            match = re.fullmatch(r'([0-9a-f]{64}):(income|balance|cashflow):([^:]+)(?::offset:(\d+))?', row.cursor_key)
            if match:
                identity, kind, symbol, offset = match.groups()
                groups[(kind, symbol)][identity].append((int(offset or 0), row.cursor_value))
        entries = []
        for (kind, symbol), versions in sorted(groups.items()):
            summaries = []
            for identity, pages in versions.items():
                contract = contracts.get(identity)
                valid = isinstance(contract, dict) and content_hash(contract) == identity and symbol in contract.get('symbols', [])
                evaluated, expected_offset, terminal = [], 0, False
                for offset, value in sorted(pages, key=lambda p: p[0]):
                    batch = batches.get(value.get('batchId'))
                    if batch is None:
                        valid = False
                        evaluated.append(('unrecorded', now, 'COLLECTION_RECORD_INVALID'))
                        continue
                    checked = utc(batch.finished_at or batch.started_at)
                    phase = value.get('status')
                    ok = phase == batch.status and batch.request_count == 1 and utc(batch.started_at) <= checked <= now
                    status = 'success' if ok and phase == 'completed' and batch.finished_at else 'failed' if ok and phase == 'failed' and batch.finished_at else 'running' if phase == 'reserved' and batch.status == 'running' and batch.finished_at is None else 'unrecorded'
                    evaluated.append((status, checked, value.get('errorType')))
                    count = value.get('rowCount')
                    if offset != expected_offset or terminal or type(count) is not int or count < 0:
                        valid = False
                    elif status == 'success':
                        # 首页/尾页行数不能证明分页结束，必须读原响应的 has_more。
                        raw = session.get(SourcePayload, value.get('rawId'))
                        try:
                            valid = valid and raw is not None and raw.batch_id == batch.id and raw.content_hash == value.get('responseContentHash') and content_hash(raw.payload) == raw.content_hash
                            data = json.loads(base64.b64decode(raw.payload['_http']['bodyBase64']))['data'] if valid else {}
                            terminal = valid and data.get('has_more') is not True
                        except (AttributeError, KeyError, TypeError, ValueError):
                            valid, terminal = False, False
                        expected_offset += count
                if not evaluated:
                    continue
                failed = next((v for v in evaluated if v[0] == 'failed'), None)
                status = 'failed' if failed else 'running' if any(v[0] == 'running' for v in evaluated) else 'success' if valid and terminal and all(v[0] == 'success' for v in evaluated) else 'unrecorded'
                checked = max(v[1] for v in evaluated)
                summaries.append((status, checked, failed[2] if failed else None if status in ('success', 'running') else 'COLLECTION_PLAN_INCOMPLETE'))
            if summaries:
                latest = max(summaries, key=lambda r: r[1])
                entries.append(record(f'collection:financial:{kind}:{symbol}', enabled, latest[0], latest[1],
                    max((r[1] for r in summaries if r[0] == 'success'), default=None), latest[2], now=now))
        return entries
