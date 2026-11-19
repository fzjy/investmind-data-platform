"""证券分红库存、采集凭证与按公告日/实际捕获日的版本查询。"""
from datetime import date
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from app.connectors.tushare.contracts import request_hash
from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from app.db.models import Dividend, IngestionBatch, SourcePayload
from app.ingestion.repositories import content_hash
from app.portfolio.api.dividend_facts import (DIVIDEND_FIELDS, captured_dividend_history,
    distribution_date, dividend_metadata, resolve_distribution_evidence)

SYNC_FIELDS = tuple(dict.fromkeys((*FIELD_REGISTRY[Dataset.DIVIDEND], 'imp_ann_date')))


def normalized_dividend(row):
    item = dict(row)
    for field in DIVIDEND_FIELDS:
        value = item.get(field)
        if field.endswith('_date') or field == 'div_listdate':
            try:
                item[field] = distribution_date(str(value)) if value not in ('', None) else None
            except ValueError:
                item[field] = str(value)
        elif field not in ('ts_code', 'div_proc'):
            try:
                number = format(Decimal(str(value)), 'f') if value not in ('', None) else None
                item[field] = number.rstrip('0').rstrip('.') if number and '.' in number else number
            except InvalidOperation:
                item[field] = str(value)
    return item


def _sync_rows(session, symbols):
    # 日常股票同步只把实施记录转入 Core；其余阶段在同批已验收的窄字段响应中。
    fields = SYNC_FIELDS
    hashes = {request_hash('dividend', {'ts_code': s}, fields): s for s in symbols}
    distinct_responses = select(func.min(SourcePayload.id)).join(IngestionBatch, IngestionBatch.id == SourcePayload.batch_id).where(
        SourcePayload.api_name == 'dividend', SourcePayload.request_hash.in_(hashes),
        IngestionBatch.dataset == Dataset.DIVIDEND.value, IngestionBatch.status == 'completed',
        IngestionBatch.source == 'tushare', IngestionBatch.rejected_count == 0).group_by(
            SourcePayload.request_hash, SourcePayload.content_hash)
    seen = set()
    for raw in session.scalars(select(SourcePayload).where(SourcePayload.id.in_(distinct_responses)).order_by(SourcePayload.id)):
        payload = raw.payload
        actual_fields = payload.get('fields', [])
        if raw.content_hash != content_hash(payload) or not set(fields) <= set(actual_fields):
            raise ValueError('DIVIDEND_SYNC_SOURCE_INVALID')
        for i, values in enumerate(payload['items'], 1):
            row = dict(zip(actual_fields, values, strict=True))
            if row['ts_code'] != hashes[raw.request_hash]:
                raise ValueError('DIVIDEND_SYNC_SYMBOL_INVALID')
            normalized = normalized_dividend(row)
            key = tuple(normalized.get(field) for field in DIVIDEND_FIELDS)
            if key in seen:
                continue
            seen.add(key)
            yield row | dict(sourceId=f'raw.source_payload:{raw.id}:row:{i}')


def inventory_dividend_rows(session, symbols):
    for r in session.scalars(select(Dividend).where(Dividend.ts_code.in_(symbols)).order_by(Dividend.id)):
        row = dividend_metadata(r)
        row.update({k: getattr(r, k) for k in ('ts_code', 'end_date', 'ann_date', 'div_proc',
            'record_date', 'ex_date', 'pay_date', 'cash_div_tax', 'cash_div')})
        yield row | dict(sourceId=f'core.dividend:{r.id}')
    yield from _sync_rows(session, symbols)


def _version(row):
    return tuple(row.get(k) for k in ('ts_code', 'end_date', 'ann_date', 'div_proc'))


def original_version_index(rows):
    versions = defaultdict(list)
    for row in rows:
        versions[_version(row)].append(row)
    return versions


def _covering_originals(row, versions):
    matches = versions.get(_version(row), ())
    if row not in matches:
        for original in matches:
            if all(row.get(k) is None or row[k] == original.get(k) for k in DIVIDEND_FIELDS):
                yield original


def covered_by_full_original(row, versions):
    return any(_covering_originals(row, versions))


def _known_date(value):
    try:
        return distribution_date(value)
    except ValueError:
        return None


def dividend_observations(session, symbol, start, end, as_of=None):
    """保留各来源的逐行事实；裁决时仅在共同字段全部相符时采用全字段原件。"""
    try:
        originals, captures = captured_dividend_history(session, symbol, as_of)
        evidence = 'observed' if captures else 'missing'
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        originals, captures, evidence = [], [], 'invalid'
    rows = list(originals)
    normalized_status = 'not_requested'
    if as_of is None:
        normalized_status = 'stored'
        try:
            rows.extend(inventory_dividend_rows(session, (symbol,)))
        except (ValueError, KeyError, TypeError):
            normalized_status = 'invalid'
    cutoff = (as_of or date.max).isoformat()
    first, last = start.isoformat(), end.isoformat()
    visible = []
    dated_in_window = []
    missing_announcements = 0
    for row in map(normalized_dividend, rows):
        announcement = _known_date(row['ann_date'])
        implementation = _known_date(row['imp_ann_date'])
        if as_of is not None and any(day > cutoff for day in (announcement, implementation) if day):
            continue
        visible.append(row)
        if announcement is None:
            missing_announcements += 1
        elif first <= announcement <= last:
            dated_in_window.append(row)

    originals_by_version = original_version_index(r for r in visible if r.get('_verifiedOriginal'))
    mirrors = defaultdict(set)
    adjudicated = []
    for row in visible:
        matches = list(_covering_originals(row, originals_by_version))
        if matches:
            for original in matches:
                mirrors[original['sourceId']].add(row['sourceId'])
        else:
            adjudicated.append(row)
    resolved = resolve_distribution_evidence(adjudicated, as_of or date.max, include_affected=True)
    for issue in resolved['issues']:
        if affected := issue.get('affectedSourceIds'):
            related = {mirror for ref in affected for mirror in mirrors.get(ref, ())}
            issue['affectedSourceIds'] = sorted(set(affected) | related)
    selected = {source for event in resolved['events'] for source in event['sourceIds']}
    data = []
    for row in dated_in_window:
        source_id = row['sourceId']
        data.append(dict({field: row[field] for field in DIVIDEND_FIELDS},
            source_id=source_id, captured_at=(row.get('_originalSource') or {}).get('capturedAt'),
            source_kind='verified_original' if row.get('_verifiedOriginal') else
            'core' if source_id.startswith('core.') else 'normalized_capture',
            selected_implementation=source_id in selected))
    data.sort(key=lambda row: (row['ann_date'], row['source_id']))
    return data, dict(evidence_status=evidence, normalized_status=normalized_status,
        history_complete=False, captures=captures,
        missing_announcement_rows=missing_announcements), resolved['issues']
