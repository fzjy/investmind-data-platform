"""按作业完成时间核验专项已捕获的观测与 Core 绑定。"""
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import ExtensionStore, JOB_NAME, RECOVERY_JOB_NAME, _job, instant


METADATA = {
    'usdcnh': {'id': 'fx.usdcnh', 'label': '离岸USD/CNH', 'unit': 'CNH/USD', 'frequency': 'daily'},
    'nominalGdp': {'id': 'cn.gdp.nominal_cumulative', 'label': '名义GDP累计值', 'unit': '亿元', 'frequency': 'quarterly'},
    'vix': {'id': 'us.volatility.vix', 'label': 'VIX', 'unit': 'index', 'frequency': 'daily'},
}


def _empty(group):
    return {**METADATA[group], 'status': 'unavailable', 'reason': 'NOT_COLLECTED',
        'executionState': 'not_collected', 'quoteSide': 'bid_close' if group == 'usdcnh' else None,
        'retrievedAt': None, 'evidenceFingerprint': None, 'points': []}


def read_extension_sources(session, as_of, *, group_id=None):
    cutoff = instant(as_of)
    store = ExtensionStore(None)
    sources = {key: _empty(key) for key in ([group_id] if group_id else METADATA)}
    statement = select(JobRun).where(JobRun.job_name.in_((JOB_NAME, RECOVERY_JOB_NAME)), JobRun.started_at <= cutoff)
    if group_id is not None:
        statement = statement.where(JobRun.details["plan"]["groupId"].as_string() == group_id)
    rows = session.scalars(statement.order_by(JobRun.started_at, JobRun.id)).all()
    # 保留强引用，绑定校验不为每个点重复往返PG；依然逐点验证。
    retained = (
        session.scalars(select(SourcePayload).join(IngestionBatch).where(
            IngestionBatch.dataset.in_(tuple(f'macro-extension:{key}' for key in sources)))).all(),
        session.scalars(select(IngestionBatch).where(IngestionBatch.id.in_([row.id for row in rows]))).all(),
        session.scalars(select(MacroObservation).where(MacroObservation.series_id.in_(
            tuple(METADATA[key]['id'] for key in sources)))).all(),
    )
    for row in rows:
        try:
            group = row.details['plan']['groupId']
            if group not in METADATA:
                raise ValueError()
            details = _job(row, group)
            store._parent(session, row)
            store._root(session, details['plan'])
            # 后来才完成/失败的作业不能倒灌到历史截止。
            if row.finished_at is not None and utc(row.finished_at) > cutoff:
                continue
            source = sources[group]
            phase = details['phase']
            source['executionState'] = phase
            source['reason'] = {'reserved': 'EXTENSION_ATTEMPT_UNCERTAIN', 'received': 'CORE_PENDING',
                'failed': row.error_message, 'completed': None}[phase]
            if phase in ('received', 'completed'):
                raw, retrieved = store._raw(session, row)
                if retrieved > cutoff or phase != 'completed':
                    continue
                evidence = raw['evidence']
                source.update(retrievedAt=retrieved.isoformat(),
                    evidenceFingerprint=evidence['evidenceFingerprint'],
                    quoteSide=details['plan']['groupPolicy']['quoteSide'],
                    points=[{'period': p['period'], 'value': p['value'],
                        'reportedRealYoy': p['diagnostics'].get('reportedRealYoy'),
                        'sourceRow': p['sourceRef']['row']} for p in evidence['observations']])
                source['status'] = 'available' if source['points'] else 'unavailable'
                source['reason'] = None if source['points'] else 'RESPONSE_EMPTY'
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ExtensionError('EXTENSION_VIEW_SOURCE_INVALID') from None
    del retained
    return sources
