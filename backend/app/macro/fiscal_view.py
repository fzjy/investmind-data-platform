"""财政月度网格与来源复算合同；只读已完成原件及Core绑定。"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import re

from sqlalchemy import select

from app.db.models.ops import JobCursor, JobRun
from app.macro.extensions.config import ExtensionError, FiscalDocument
from app.macro.extensions.ingest import utc
from app.macro.extensions.normalize import decimal_text, number
from app.macro.extensions.plan import canonical, validate_extension_plan
from app.macro.extensions.store import instant
from app.macro.fiscal_official import parse_government_bond_table
from app.macro.fiscal_plan import month_label
from app.macro.fiscal_store import FiscalStore, JOB, ERRORS


def _month(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed.day != 1 or parsed.year < 1000:
        raise ValueError()
    return parsed.year * 12 + parsed.month - 1


def rebuild_fiscal_view(window, documents, latest_attempt):
    if (type(window) is not dict or set(window) != {'startPeriod', 'endPeriod', 'requestedPeriods', 'expectedDocuments', 'planFingerprint', 'scheduledAt', 'frozenPlan'}
        or type(window['requestedPeriods']) is not int or not 1 <= window['requestedPeriods'] <= 48
        or not re.fullmatch('[0-9a-f]{64}', window['planFingerprint'])
        or datetime.fromisoformat(window['scheduledAt']).utcoffset() is None):
        raise ValueError()
    plan = window['frozenPlan']
    validate_extension_plan(plan)
    if (plan['groupId'] != 'governmentBonds' or any(window[k] != plan[k] for k in ('startPeriod', 'endPeriod', 'scheduledAt'))
        or window['requestedPeriods'] != plan['groupPolicy']['lookbackPeriods']
        or window['expectedDocuments'] != plan['documents'] or window['planFingerprint'] != plan['fingerprint']):
        raise ValueError()
    start, end = _month(window['startPeriod']), _month(window['endPeriod'])
    if end - start + 1 != window['requestedPeriods']:
        raise ValueError()
    expected = [FiscalDocument.model_validate(d).model_dump() for d in window['expectedDocuments']]
    if (not 1 <= len(expected) <= 5 or [d['year'] for d in expected] != list(range(start // 12, end // 12 + 1))
        or len({d['url'] for d in expected}) != len(expected) or type(documents) is not list or len(documents) > len(expected)):
        raise ValueError()
    values, seen_years = {}, set()
    for document in documents:
        if (type(document) is not dict or set(document) != {'year', 'url', 'rawSha256', 'retrievedAt', 'points'}
            or {'year': document['year'], 'url': document['url']} not in expected or document['year'] in seen_years
            or not re.fullmatch('[0-9a-f]{64}', document['rawSha256']) or type(document['points']) is not list
            or len(document['points']) > 12):
            raise ValueError()
        retrieved = instant(datetime.fromisoformat(document['retrievedAt']))
        if retrieved.isoformat() != document['retrievedAt']:
            raise ValueError()
        seen_years.add(document['year']); previous = ''
        for point in document['points']:
            if type(point) is not dict or set(point) != {'period', 'value'} or type(point['value']) is not str:
                raise ValueError()
            index = _month(point['period']); value = number(point['value'])
            if (index // 12 != document['year'] or point['period'] <= previous
                or decimal_text(value) != point['value'] or abs(value) >= Decimal('1e18')):
                raise ValueError()
            previous = point['period']
            if start <= index <= end:
                values[point['period']] = float(value) if value else 0.0
    if [d['year'] for d in documents] != sorted(seen_years):
        raise ValueError()
    if latest_attempt is not None:
        if (type(latest_attempt) is not dict or set(latest_attempt) != {'scheduledAt', 'status', 'errorCode'}
            or latest_attempt['status'] not in ('completed', 'partial', 'pending', 'failed')
            or datetime.fromisoformat(latest_attempt['scheduledAt']).utcoffset() is None
            or (latest_attempt['errorCode'] not in ERRORS if latest_attempt['status'] == 'failed' else latest_attempt['errorCode'] is not None)):
            raise ValueError()
    labels = [month_label(n) for n in range(start, end + 1)]
    points = [{'month': m, 'value': values.get(m)} for m in labels]
    missing = [m for m in labels if m not in values]
    latest = next((p for p in reversed(points) if p['value'] is not None), None)
    return {'schemaVersion': '1.0', 'algorithmVersion': 'government-bond-flow-v1',
        'source': 'pboc', 'unit': '亿元', 'frequency': 'monthly',
        'basis': 'retrieval-visible-restated-history-not-strict-pit',
        'window': deepcopy(window), 'documents': deepcopy(documents), 'points': points,
        'missingPeriods': missing, 'latest': latest,
        'status': 'available' if not missing else 'partial' if latest else 'unavailable',
        'latestAttempt': deepcopy(latest_attempt)}


def build_fiscal_view(plan, documents, latest_attempt):
    return rebuild_fiscal_view({'startPeriod': plan['startPeriod'], 'endPeriod': plan['endPeriod'],
        'requestedPeriods': plan['groupPolicy']['lookbackPeriods'], 'expectedDocuments': plan['documents'],
        'planFingerprint': plan['fingerprint'], 'scheduledAt': plan['scheduledAt'], 'frozenPlan': deepcopy(plan)}, documents, latest_attempt)


def fiscal_view_valid(value):
    try:
        return type(value) is dict and canonical(value) == canonical(rebuild_fiscal_view(value['window'], value['documents'], value['latestAttempt']))
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, OverflowError):
        return False


def load_fiscal_inputs(factory, as_of):
    cutoff, store = instant(as_of), FiscalStore(factory)
    candidates, latest_attempt = [], None
    with factory() as session:
        roots = session.scalars(select(JobCursor).where(JobCursor.job_name == JOB).order_by(JobCursor.cursor_key)).all()
        for root in roots:
            if type(root.cursor_value) is not dict or set(root.cursor_value) != {'plan'}:
                raise ExtensionError('EXTENSION_VIEW_SOURCE_INVALID')
            plan = root.cursor_value['plan']; store._root(session, plan)
            if instant(datetime.fromisoformat(plan['scheduledAt'])) > cutoff:
                continue
            jobs = session.scalars(select(JobRun).where(JobRun.job_name == JOB, JobRun.started_at <= cutoff)).all()
            jobs = [r for r in jobs if r.details.get('plan', {}).get('scheduledAt') == plan['scheduledAt']]
            if not jobs:
                continue
            documents, phases, errors = [], [], []
            for row in sorted(jobs, key=lambda r: r.details['document']['year']):
                if row.finished_at is not None and utc(row.finished_at) > cutoff:
                    phases.append('received'); continue
                d = store._run(session, row); phase = store.effective_phase(session, row, cutoff); phases.append(phase)
                if phase == 'failed':
                    errors.append(row.error_message)
                if phase == 'completed':
                    body, retrieved = store._raw(session, row)
                    if retrieved > cutoff:
                        continue
                    parsed = parse_government_bond_table(body, d['document']['year'])
                    documents.append({**d['document'], 'rawSha256': parsed['rawSha256'], 'retrievedAt': retrieved.isoformat(), 'points': parsed['points']})
            status = 'failed' if errors else 'pending' if any(p in ('reserved', 'received') for p in phases) else 'completed' if len(jobs) == len(plan['documents']) else 'partial'
            latest_attempt = {'scheduledAt': plan['scheduledAt'], 'status': status, 'errorCode': errors[0] if errors else None}
            candidates.append(build_fiscal_view(plan, documents, latest_attempt))
    if not candidates:
        return None
    # 最新未完整/失败时先保留最近完整可见结果；同时明确最新尝试状态。
    chosen = next((c for c in reversed(candidates) if c['status'] == 'available'), candidates[-1])
    return rebuild_fiscal_view(chosen['window'], chosen['documents'], latest_attempt)
