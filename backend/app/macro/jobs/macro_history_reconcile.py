"""已完成NBS回填的显式零网络对账；不发布、不修改core或旧审计。"""
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.connectors.macro.nbs_history import SERIES_IDS
from app.connectors.macro.nbs_release_feed import _release_url_allowed
from app.db.models import JobCursor, JobFailure, JobRun, MacroObservation, MacroSeriesState
from app.db.session import create_engine_and_session
from app.jobs.locks import JobLock, data_publication_lock_key, job_lock_for_engine
from app.macro.jobs.macro_history_backfill import CURSOR_KEY, _Checkpoint, _report
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.jobs.cursors import SqlJobCursorRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, catalog_hashes_match
from app.macro.history_catalog import bind_history_catalog
from app.macro.freshness import observation_expired


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _audit_time(details: dict[str, object]) -> datetime | None:
    raw = details.get('asOf')
    try:
        parsed = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return _utc(parsed) if parsed is not None and parsed.utcoffset() is not None else None


def _months(series_id: str, windows: object, start: date, end: date) -> set[date]:
    if not isinstance(windows, list) or not windows:
        raise ValueError('NBS_HISTORY_RECONCILE_EVIDENCE_INVALID')
    months = set()
    for window in windows:
        try:
            first, last = date.fromisoformat(window['start']), date.fromisoformat(window['end'])
        except (TypeError, KeyError, ValueError):
            raise ValueError('NBS_HISTORY_RECONCILE_EVIDENCE_INVALID') from None
        if first > last or first.replace(day=1) < start or last.replace(day=1) > end:
            raise ValueError('NBS_HISTORY_RECONCILE_EVIDENCE_INVALID')
        # 历史窗口的月末下界也包括该统计月，不能漏掉月初观测。
        period = first.replace(day=1)
        while period <= last:
            if series_id != 'cn.industry.value_added_yoy' or period.month not in (1, 2):
                months.add(period)
            period = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


class MacroHistoryReconcileJob:
    def __init__(self, factory: sessionmaker[Session], catalog: MacroSeriesCatalog, lock: JobLock,
                 *, timezone: str = 'Asia/Shanghai') -> None:
        self._factory, self._catalog, self._lock = factory, catalog, lock
        self._timezone = ZoneInfo(timezone)

    def run(self, *, as_of: datetime, history_years: int, end: date | None) -> dict[str, object]:
        if as_of.utcoffset() is None:
            raise ValueError('NBS_HISTORY_CONFIG_INVALID')
        key = data_publication_lock_key()
        if not self._lock.try_acquire(key):
            return {'status': 'locked', 'requestCount': 0, 'rowCount': 0}
        try:
            return self._run(_utc(as_of), history_years, end)
        finally:
            self._lock.release(key)

    def _run(self, as_of: datetime, history_years: int, end: date | None) -> dict[str, object]:
        catalog = self._catalog
        business_date = as_of.astimezone(self._timezone).date()
        history = SqlMacroSyncHistory(self._factory)
        business_key = f'reconcile-nbs-history:{as_of.isoformat()}'
        history.inspect(business_key, catalog.catalog_version, catalog.catalog_hash)
        snapshot = history.gap_snapshot(catalog.catalog_version, catalog.catalog_hash)
        targets = tuple(sid for sid in SERIES_IDS if snapshot is not None and sid in snapshot.details['unresolvedGaps'])
        if not targets:
            return {'status': 'no_change', 'requestCount': 0, 'rowCount': 0, 'reconciledSeries': []}
        source_time = _audit_time(snapshot.details)
        if source_time is None or source_time > as_of:
            raise ValueError('NBS_HISTORY_RECONCILE_EVIDENCE_INVALID')
        # 状态、失败解决标记和新的gap快照必须同一事务提交；中断全部回滚。
        with self._factory.begin() as session:
            started_at = datetime.now(UTC)
            cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == 'backfill-macro-history',
                JobCursor.cursor_key == CURSOR_KEY))
            try:
                checkpoint = _Checkpoint.model_validate(cursor.cursor_value if cursor else None)
            except ValueError:
                raise ValueError('NBS_HISTORY_CHECKPOINT_INVALID') from None
            if (checkpoint.catalog_version != catalog.catalog_version
                    or not catalog_hashes_match(checkpoint.catalog_hash, catalog.catalog_hash)
                    or checkpoint.history_years != history_years or (end is not None and checkpoint.end != end)):
                raise ValueError('NBS_HISTORY_CONFIG_MISMATCH')
            if checkpoint.as_of > as_of or _report(checkpoint)['status'] != 'completed':
                raise ValueError('NBS_HISTORY_RECONCILE_INCOMPLETE')
            rows = session.scalars(select(MacroObservation).where(MacroObservation.source == 'nbs',
                MacroObservation.series_id.in_(targets))).all()
            active = [row for row in rows if row.observed_at <= business_date and _utc(row.released_at) <= as_of
                and (row.valid_until is None or _utc(row.valid_until) > as_of)]
            by_month = defaultdict(list)
            for row in active:
                by_month[(row.series_id, row.observed_at)].append(row)
            entries = defaultdict(list)
            for entry in checkpoint.entries:
                entries[(entry.release.series_id, entry.release.observed_at)].append(entry)
            coverage = {}
            newest = {}
            states = {}
            verified = {}
            for sid in targets:
                definition = catalog.require(sid)
                months = _months(sid, snapshot.details['unresolvedGaps'][sid], checkpoint.start, checkpoint.end)
                if not months or any(not checkpoint.start <= month <= checkpoint.end for month in months):
                    raise ValueError('NBS_HISTORY_RECONCILE_INCOMPLETE')
                confirmed_months = months | {period for (entry_sid, period) in entries if entry_sid == sid}
                for month in confirmed_months:
                    candidates, evidence = by_month[(sid, month)], entries[(sid, month)]
                    if len(candidates) != 1 or not evidence:
                        raise ValueError('NBS_HISTORY_RECONCILE_INCOMPLETE')
                    row = candidates[0]
                    if (any(getattr(row, field) != getattr(definition, field)
                            for field in ('region', 'frequency', 'unit', 'seasonal_adjustment'))
                            or row.source_url not in {entry.release.url for entry in evidence}
                            or any(row.value != Decimal(entry.value) or _utc(row.released_at) != _utc(entry.released_at) for entry in evidence)):
                        raise ValueError('NBS_HISTORY_RECONCILE_INCOMPLETE')
                latest = max((r for r in active if r.series_id == sid), key=lambda r: (r.observed_at, _utc(r.released_at)))
                if (any(getattr(latest, field) != getattr(definition, field) for field in ('region', 'frequency', 'unit', 'seasonal_adjustment'))
                        or len(by_month[(sid, latest.observed_at)]) != 1 or not latest.value.is_finite()
                        or latest.observed_at.day != 1 or not _release_url_allowed(latest.source_url)):
                    raise ValueError('NBS_HISTORY_RECONCILE_INCOMPLETE')
                state = session.get(MacroSeriesState, sid)
                if state is not None and (state.catalog_version != catalog.catalog_version
                        or (state.latest_observed_at is not None and state.latest_observed_at > business_date)
                        or any(time is not None and _utc(time) > as_of
                            for time in (state.last_checked_at, state.last_success_at, state.latest_released_at))):
                    raise ValueError('NBS_HISTORY_RECONCILE_EVIDENCE_INVALID')
                newest[sid], states[sid] = latest, state
                verified[sid] = confirmed_months
                coverage[sid] = {'expected': len(months), 'covered': len(months), 'checkpointVerified': len(confirmed_months)}
            declared = deepcopy(snapshot.details['unresolvedGaps'])
            for sid in targets:
                declared.pop(sid)
                state = states[sid]
                # 历史对账不能掩盖独立TIMEOUT等来源故障。
                if (state is None or state.last_error_code not in (None, 'MACRO_HISTORY_GAP')
                        or (state.last_error_code != 'MACRO_HISTORY_GAP' and state.stale_reason != 'MACRO_HISTORY_GAP')):
                    continue
                latest = newest[sid]
                expired = observation_expired(catalog.require(sid), latest.observed_at, business_date)
                state.catalog_version, state.active_source, state.fallback_used = catalog.catalog_version, 'nbs', False
                state.latest_observed_at, state.latest_released_at = latest.observed_at, _utc(latest.released_at)
                state.last_checked_at, state.updated_at, state.stale = as_of, as_of, expired
                state.stale_reason = state.last_error_code = 'MACRO_VALUE_EXPIRED' if expired else None
                state.last_error_message = state.last_error_code
                if not expired:
                    state.last_success_at, state.consecutive_failures = as_of, 0
            failures = session.execute(select(JobFailure, JobRun).join(JobRun, JobRun.id == JobFailure.job_run_id).where(
                JobRun.job_name == 'sync-macro', JobFailure.dataset.in_([f'macro:{sid}' for sid in targets]),
                JobFailure.request_key == 'nbs', JobFailure.error_code == 'MACRO_HISTORY_GAP', JobFailure.resolved_at.is_(None)))
            for failure, run in failures:
                failed_at = _audit_time(run.details)
                if (run.details.get('catalogVersion') == catalog.catalog_version
                        and catalog_hashes_match(run.details.get('catalogHash'), catalog.catalog_hash)
                        and failed_at is not None and failed_at <= as_of):
                    sid = failure.dataset.removeprefix('macro:')
                    declared_failure = run.details.get('unresolvedGaps')
                    try:
                        failed_months = _months(sid, declared_failure.get(sid) if isinstance(declared_failure, dict) else None,
                            checkpoint.start, checkpoint.end)
                    except ValueError:
                        continue  # 未知/非法/超范围证据不能静默解决。
                    if failed_months and failed_months <= verified[sid]:
                        failure.resolved_at = as_of
            run_id = str(uuid4())
            session.add(JobRun(id=run_id, job_name='sync-macro', business_key=business_key, status='completed',
                started_at=started_at, finished_at=datetime.now(UTC), request_count=0, row_count=0, details={
                    'operation': 'nbs-history-reconciliation', 'asOf': as_of.isoformat(),
                    'catalogVersion': catalog.catalog_version, 'catalogHash': catalog.catalog_hash,
                    'trigger': 'cli', 'attempt': 1, 'sourceRunId': snapshot.id,
                    'backfillAsOf': checkpoint.as_of.isoformat(), 'backfillCatalogHash': checkpoint.catalog_hash,
                    'previousUnresolvedGaps': deepcopy(snapshot.details['unresolvedGaps']),
                    'unresolvedGaps': declared, 'currentUnresolvedGaps': deepcopy(declared),
                    'coverage': coverage, 'reconciledSeries': list(targets),
                    'requestCount': 0, 'rowCount': 0,
                }))
        return {'status': 'completed', 'requestCount': 0, 'rowCount': 0, 'runId': run_id,
                'reconciledSeries': list(targets), 'coverage': coverage, 'published': False}


def reconcile_macro_history(settings: Settings, as_of: datetime) -> dict[str, object]:
    if not settings.database_url:
        raise ValueError('NBS_HISTORY_DATABASE_REQUIRED')
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    engine, factory = create_engine_and_session(settings.database_url)
    try:
        catalog = bind_history_catalog(catalog,
            SqlJobCursorRepository(factory, job_name='backfill-macro-history').load(CURSOR_KEY))
        return MacroHistoryReconcileJob(factory, catalog, job_lock_for_engine(engine), timezone=settings.sync_timezone).run(as_of=as_of,
            history_years=settings.macro_history_years, end=settings.macro_backfill_end)
    finally:
        engine.dispose()
