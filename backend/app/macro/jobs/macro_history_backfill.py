"""有界 NBS 历史队列；checkpoint 在 core commit 后确认，崩溃可幂等恢复。"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Self
from uuid import uuid4

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.contracts import MacroFetchWindow, ProviderError, ProviderFetchResult, ProviderObservation, ProviderSchemaError
from app.connectors.macro.nbs import NbsTextTransport
from app.connectors.macro.nbs_history import (
    SERIES_IDS, NbsReleaseLocator, discover_history_page, parse_history_release,
)
from app.connectors.macro.nbs_release_feed import INDEX, _bounded_failure_context, _response_summary, _validate
from app.db.models import JobRun
from app.jobs.cursors import CursorStore
from app.jobs.locks import JobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import MacroSeriesCatalog, catalog_hashes_match


CURSOR_KEY = 'nbs-history-v1'


class _Entry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    release: NbsReleaseLocator
    status: Literal['pending', 'done'] = 'pending'
    value: str | None = None
    released_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r'^[A-Z0-9_]+$')

    @model_validator(mode='after')
    def validate_done(self) -> Self:
        if self.status == 'done':
            try:
                if self.value is None or not Decimal(self.value).is_finite():
                    raise ValueError('invalid value')
            except InvalidOperation:
                raise ValueError('invalid value') from None
            if self.released_at is None or self.released_at.utcoffset() is None or self.error_code is not None:
                raise ValueError('invalid done entry')
        elif self.value is not None or self.released_at is not None:
            raise ValueError('invalid pending entry')
        return self


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['1.0'] = '1.0'
    catalog_version: str
    catalog_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    start: date
    end: date
    as_of: datetime
    history_years: int = Field(ge=1, le=30)
    next_index_page: int = Field(default=0, ge=0, le=1000)
    page_count: int = Field(default=1, ge=1, le=1000)
    anchor_fingerprint: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    discovery_complete: bool = False
    report_steps_since_index: int = Field(default=0, ge=0, le=100)
    entries: list[_Entry] = Field(default_factory=list, max_length=10000)

    @model_validator(mode='after')
    def validate_consistency(self) -> Self:
        if (self.as_of.utcoffset() is None or self.start.day != 1
                or self.end.day != monthrange(self.end.year, self.end.month)[1]
                or self.start > self.end or self.end >= self.as_of.date().replace(day=1)
                or self.start != _shift_month(self.end, -12 * self.history_years + 1)
                or self.next_index_page > self.page_count
                or self.discovery_complete != (self.next_index_page == self.page_count)
                or (self.next_index_page > 0) != (self.anchor_fingerprint is not None)):
            raise ValueError('invalid checkpoint boundaries')
        keys = set()
        confirmed: dict[tuple[str, date], tuple[Decimal, datetime]] = {}
        for entry in self.entries:
            if entry.release.key in keys or not self.start <= entry.release.observed_at <= self.end:
                raise ValueError('invalid checkpoint release')
            if entry.released_at is not None and entry.released_at > self.as_of:
                raise ValueError('future checkpoint release')
            if entry.status == 'done':
                period_key = (entry.release.series_id, entry.release.observed_at)
                identity = (Decimal(entry.value), entry.released_at)
                if period_key in confirmed and confirmed[period_key] != identity:
                    raise ValueError('conflicting checkpoint evidence')
                confirmed[period_key] = identity
            keys.add(entry.release.key)
        return self


def _load(cursors: CursorStore) -> _Checkpoint | None:
    raw = cursors.load(CURSOR_KEY)
    if raw is None:
        return None
    try:
        return _Checkpoint.model_validate(raw)
    except ValueError:
        raise ValueError('NBS_HISTORY_CHECKPOINT_INVALID') from None


def _shift_month(value: date, months: int) -> date:
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return date(year, month + 1, 1)


def _report(state: _Checkpoint, *, requests: int = 0, rows: int = 0) -> dict[str, object]:
    covered = {(e.release.series_id, e.release.observed_at) for e in state.entries if e.status == 'done'}
    # 冲突/失败副本不能被同月已成功报告掩盖。
    blocked = {(e.release.series_id, e.release.observed_at) for e in state.entries if e.status != 'done'}
    missing: dict[str, list[str]] = {series_id: [] for series_id in SERIES_IDS}
    period = state.start
    while period <= state.end:
        for series_id in SERIES_IDS:
            if series_id == 'cn.industry.value_added_yoy' and period.month in (1, 2):
                continue
            if (series_id, period) not in covered or (series_id, period) in blocked:
                missing[series_id].append(period.strftime('%Y-%m'))
        period = _shift_month(period, 1)
    pending = sum(e.status != 'done' for e in state.entries)
    complete = state.discovery_complete and pending == 0 and not any(missing.values())
    return {'status': 'completed' if complete else 'partial', 'requestCount': requests,
            'rowCount': rows, 'start': state.start.isoformat(), 'end': state.end.isoformat(),
            'asOf': state.as_of.isoformat(), 'nextIndexPage': state.next_index_page,
            'discoveryComplete': state.discovery_complete, 'pendingReleases': pending,
            'missingMonths': missing,
            'failures': [{'seriesId': e.release.series_id, 'url': e.release.url, 'code': e.error_code}
                         for e in state.entries if e.error_code is not None]}


def macro_history_status(cursors: CursorStore) -> dict[str, object]:
    state = _load(cursors)
    return {'status': 'not_started', 'requestCount': 0, 'rowCount': 0} if state is None else _report(state)


class SqlMacroHistoryRuns:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def start(self, state: _Checkpoint) -> str:
        run_id = str(uuid4())
        with self._factory.begin() as session:
            session.add(JobRun(id=run_id, job_name='backfill-macro-history', business_key=CURSOR_KEY,
                               status='running', details={'catalogVersion': state.catalog_version,
                                   'catalogHash': state.catalog_hash, 'asOf': state.as_of.isoformat()}))
        return run_id

    def finish(self, run_id: str, report: dict[str, object]) -> None:
        with self._factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise LookupError('NBS_HISTORY_RUN_MISSING')
            run.status = str(report['status'])
            run.request_count = int(report['requestCount'])
            run.row_count = int(report['rowCount'])
            run.details = {**run.details, **report}
            run.finished_at = datetime.now(UTC)


class MacroHistoryBackfillJob:
    def __init__(self, *, catalog: MacroSeriesCatalog, transport: NbsTextTransport,
                 cursors: CursorStore, observations: MacroObservationRepository,
                 history: SqlMacroHistoryRuns, lock: JobLock,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._catalog, self._transport, self._cursors = catalog, transport, cursors
        self._observations, self._history, self._lock = observations, history, lock
        self._clock = clock or (lambda: datetime.now(UTC))
        self._series = {sid: catalog.require(sid) for sid in SERIES_IDS}
        self._sources = {sid: next(s for s in series.sources if s.provider == 'nbs')
                         for sid, series in self._series.items()}
        for sid in SERIES_IDS:
            _validate(self._series[sid], self._sources[sid])

    def status(self) -> dict[str, object]:
        return macro_history_status(self._cursors)

    def _save(self, state: _Checkpoint) -> None:
        value = state.model_dump(mode='json')
        _Checkpoint.model_validate(value)
        self._cursors.save(CURSOR_KEY, value)

    def _merge(self, state: _Checkpoint, releases: tuple[NbsReleaseLocator, ...]) -> None:
        entries = {e.release.key: e for e in state.entries}
        for release in releases:
            # model_copy 绕过 Pydantic 校验的对象也不得成为请求目标。
            release = NbsReleaseLocator.model_validate(release.model_dump())
            existing = entries.get(release.key)
            if existing is not None and existing.release != release:
                raise ValueError('NBS_HISTORY_MANIFEST_CONFLICT')
            if not state.start <= release.observed_at <= state.end:
                continue
            if existing is None:
                entry = _Entry(release=release)
                state.entries.append(entry)
                entries[release.key] = entry

    def run(self, *, as_of: datetime, history_years: int, end: date | None,
            request_limit: int, releases: tuple[NbsReleaseLocator, ...]) -> dict[str, object]:
        if as_of.utcoffset() is None or not 1 <= history_years <= 30 or not 2 <= request_limit <= 100:
            raise ValueError('NBS_HISTORY_CONFIG_INVALID')
        key = data_publication_lock_key()
        if not self._lock.try_acquire(key):
            return {'status': 'locked', 'requestCount': 0, 'rowCount': 0}
        try:
            state = _load(self._cursors)
            if state is None:
                final_month = _shift_month(as_of.date(), -2)
                target_end = end or final_month.replace(day=monthrange(final_month.year, final_month.month)[1])
                state = _Checkpoint(catalog_version=self._catalog.catalog_version,
                                    catalog_hash=self._catalog.catalog_hash,
                                    start=_shift_month(target_end, -12 * history_years + 1),
                                    end=target_end, as_of=as_of, history_years=history_years)
            elif (not catalog_hashes_match(state.catalog_hash, self._catalog.catalog_hash)
                  or state.catalog_version != self._catalog.catalog_version
                  or state.history_years != history_years or (end is not None and state.end != end)):
                raise ValueError('NBS_HISTORY_CONFIG_MISMATCH')
            self._merge(state, releases)
            # 日常同步、修订或外部恢复可能使先前 done 的 core 不再有效。
            # 持 publication 锁重查实际证据；只读 status 保留历史证据语义。
            for entry in state.entries:
                if entry.status != 'done':
                    continue
                observation = ProviderObservation('nbs', str(self._sources[entry.release.series_id].series),
                    entry.release.observed_at, entry.released_at, None, Decimal(entry.value), entry.release.url)
                try:
                    self._observations.ensure_history_observation(self._series[entry.release.series_id],
                        observation, state.as_of, require_present=True)
                except ProviderSchemaError as exc:
                    entry.status, entry.value, entry.released_at = 'pending', None, None
                    entry.error_code = exc.code
            self._save(state)
            return self._batch(state, request_limit)
        finally:
            self._lock.release(key)

    def _batch(self, state: _Checkpoint, limit: int) -> dict[str, object]:
        run_id = self._history.start(state)
        requests = rows = 0
        attempted: set[str] = set()
        anchor_checked = state.next_index_page == 0
        recover_from_home = False
        requested_indexes: set[str] = set()
        diagnostics: list[dict[str, object]] = []
        window = MacroFetchWindow(state.start, state.end, state.as_of)
        while requests < limit:
            entry = next((e for e in state.entries if e.status == 'pending' and e.release.key not in attempted), None)
            if (not state.discovery_complete or not anchor_checked) and state.report_steps_since_index >= limit:
                entry = None  # 失败队列不能永久阻止发现后续目录。
            if entry is None and state.discovery_complete and anchor_checked:
                break
            if entry is not None:
                attempted.add(entry.release.key)
                state.report_steps_since_index += 1
                url = entry.release.url
                context: dict[str, object] = {'stage': 'release', 'url': url, 'responseReceived': False}
            else:
                page = 0 if recover_from_home else state.next_index_page if anchor_checked else state.next_index_page - 1
                url = INDEX if page == 0 else INDEX + f'index_{page}.html'
                context = {'stage': 'index', 'url': url, 'pageIndex': page, 'responseReceived': False}
                requested_indexes.add(url)
            requests += 1  # runtime 使用单次尝试 transport；失败也消耗预算。
            result: ProviderFetchResult | None = None
            batch_id = None
            try:
                response = self._transport.get_text(url)
                if response.request_count != 1:
                    raise ProviderSchemaError('NBS_HISTORY_TRANSPORT_INVALID')
                context.update(_response_summary(response.text))
                if entry is None:
                    count, discovered, fingerprint = discover_history_page(response.text, page, state.start, state.end,
                        known_releases=tuple(item.release for item in state.entries))
                    self._merge(state, discovered)
                    if recover_from_home:
                        # 只在有效官方首页证据下恢复；保留队列与成功月份。
                        state.next_index_page = 1
                        state.anchor_fingerprint = fingerprint
                        state.report_steps_since_index = 0
                        anchor_checked, recover_from_home = True, False
                    elif not anchor_checked:
                        anchor_checked = True
                        if fingerprint != state.anchor_fingerprint or count != state.page_count:
                            state.next_index_page = 0
                            state.anchor_fingerprint = None
                        elif state.discovery_complete:
                            state.report_steps_since_index = 0
                    else:
                        state.next_index_page = page + 1
                        state.anchor_fingerprint = fingerprint
                        state.report_steps_since_index = 0
                    state.page_count = count
                    state.discovery_complete = state.next_index_page == count
                    self._save(state)
                    continue
                series = self._series[entry.release.series_id]
                # raw 请求身份必须包含本篇 URL；否则同一批同窗口的报告共用
                # audit_request 的确定性 batch_id，后续报告证据会被第一篇吞掉。
                source = self._sources[series.id].model_copy(update={'source_url': AnyHttpUrl(url)})
                observation = parse_history_release(entry.release, source, response.text)
                if observation.released_at is None or observation.released_at > state.as_of:
                    raise ProviderSchemaError('NBS_HISTORY_RELEASE_NOT_VISIBLE')
                incumbent = next((e for e in state.entries if e.status == 'done'
                                  and e.release.series_id == series.id
                                  and e.release.observed_at == observation.observed_at), None)
                if incumbent is not None and (Decimal(incumbent.value) != observation.value
                                               or incumbent.released_at != observation.released_at):
                    raise ProviderSchemaError('NBS_DUPLICATE_PERIOD')
                self._observations.ensure_history_observation(series, observation, state.as_of)
                result = ProviderFetchResult('nbs', 'success', (observation,), context, 1)
                batch_id = self._observations.audit_request(series, source, window, result, self._clock(), run_id)
                persisted = self._observations.persist(series, result, self._clock(), batch_id=batch_id)
                if persisted.rejected or persisted.inserted + persisted.unchanged != 1:
                    raise ProviderSchemaError('NBS_HISTORY_ROWS_REJECTED')
                self._observations.ensure_history_observation(series, observation, state.as_of, require_present=True)
                rows += persisted.inserted
                entry.value, entry.released_at = str(observation.value), observation.released_at
                entry.status, entry.error_code = 'done', None
                self._save(state)
            except ProviderError as exc:
                context['errorCode'] = exc.code
                safe = _bounded_failure_context(context)
                diagnostics.append(safe)
                if entry is not None:
                    entry.error_code = exc.code
                    state.entries.remove(entry)
                    state.entries.append(entry)  # 跨批轮转；坏报告不饿死后面的好报告。
                    if batch_id is None:
                        failed = ProviderFetchResult('nbs', 'failed', (), {'failureContext': safe}, 1, exc)
                        self._observations.audit_request(self._series[entry.release.series_id],
                            self._sources[entry.release.series_id].model_copy(update={'source_url': AnyHttpUrl(url)}),
                            window, failed, self._clock(), run_id)
                    self._save(state)
                else:
                    if (not anchor_checked and url != INDEX and INDEX not in requested_indexes
                            and (not isinstance(exc, ProviderSchemaError) or exc.code == 'NBS_INDEX_SCHEMA_INVALID')):
                        # 锚点页消失/分页缩短：剩余预算内最多核对首页一次。
                        # 身份冲突/非法transport不能靠换页规避，双失败不改进度。
                        recover_from_home = True
                        continue
                    break  # 同一失败目录不能在同批无限重试；游标不推进。
        report = {**_report(state, requests=requests, rows=rows), 'jobRunId': run_id,
                  'failureContexts': diagnostics}
        # 已保存的覆盖证据不能替代本批目录复核；失败或预算用尽可续跑，
        # 但不能让 CLI 以退出码 0 宣告本批成功。
        if report['status'] == 'completed' and (not anchor_checked or diagnostics):
            report['status'] = 'partial'
        self._history.finish(run_id, report)
        return report
