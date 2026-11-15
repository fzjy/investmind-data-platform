"""独立国债月度队列，已确认月份只重放本地证据，不重抓 HTTP。"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config_paths import CONFIG_ROOT
from app.connectors.macro.chinamoney_govbond import GovbondTransport, fetch_govbond_history
from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult
from app.jobs.cursors import CursorStore
from app.macro.jobs.govbond_history_repository import GovbondHistoryRepository, fingerprint
from app.jobs.locks import JobLock, data_publication_lock_key
from app.macro.catalog import MacroSeriesCatalog, MacroSourceDefinition, catalog_hashes_match

CURSOR_KEY = 'govbond-history-v1'
SERIES_ID = 'cn.rate.govbond_10y'
DEFAULT_HISTORY_SOURCES_FILE = CONFIG_ROOT / "macro/macro-history-sources.json"
CHINA_TIME = timezone(timedelta(hours=8))
_PUBLIC_ERRORS = frozenset({
    'GOVBOND_HISTORY_EMPTY_MONTH', 'GOVBOND_HISTORY_EVIDENCE_INVALID', 'GOVBOND_HISTORY_FETCH_FAILED',
    'HTTP_REQUEST_FAILED', 'HTTP_AUTH_FAILED', 'HTTP_PERMISSION_DENIED', 'HTTP_RATE_LIMITED',
    'HTTP_SERVER_ERROR', 'HTTP_CLIENT_ERROR', 'HTTP_RETRY_EXHAUSTED', 'HTTP_JSON_INVALID',
    'HTTP_JSON_OBJECT_REQUIRED', 'CHINAMONEY_IDENTIFIER_INVALID', 'CHINAMONEY_HOST_NOT_ALLOWED',
    'CHINAMONEY_PARAMS_INVALID', 'CHINAMONEY_WINDOW_INVALID', 'CHINAMONEY_HEADER_INVALID',
    'CHINAMONEY_PAGINATION_INVALID', 'CHINAMONEY_PAGE_BUDGET_EXCEEDED', 'CHINAMONEY_SCHEMA_INVALID',
})


def _public_error(code: str | None) -> str | None:
    return None if code is None else code if code in _PUBLIC_ERRORS else 'GOVBOND_HISTORY_FETCH_FAILED'


def load_govbond_source(path: Path = DEFAULT_HISTORY_SOURCES_FILE) -> MacroSourceDefinition:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if set(raw) != {'schemaVersion', 'sources'} or raw['schemaVersion'] != '1.0':
            raise ValueError('invalid manifest')
        if set(raw['sources']) not in ({SERIES_ID}, {SERIES_ID, 'cn.rate.dr007'},
                                      {SERIES_ID, 'cn.rate.dr007', 'cn.fx.usdcny'}):
            raise ValueError('invalid source')
        source = MacroSourceDefinition.model_validate(raw['sources'][SERIES_ID])
        if (source.provider != 'chinamoney' or source.series != 'CGB-10Y'
                or source.api != 'SddsIntrRateGovYldHis' or source.field != 'tenRate'
                or source.transform != 'identity' or source.access_mode != 'public_page'
                or str(source.source_url) != 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/SddsIntrRateGovYldHis'
                or source.response_fields != ('dateString', 'tenRate') or source.params != {'maxPages': '3'}):
            raise ValueError('invalid source')
        return source
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('GOVBOND_HISTORY_SOURCE_INVALID') from None


class _Point(BaseModel):
    model_config = ConfigDict(extra='forbid')
    observed_at: date
    value: str
    revision: str = Field(min_length=1, max_length=64)
    released_at: datetime

    @model_validator(mode='after')
    def validate_value(self) -> Self:
        try:
            finite = Decimal(self.value).is_finite()
        except InvalidOperation:
            finite = False
        if not finite or self.released_at.utcoffset() is None:
            raise ValueError('invalid point')
        return self


class _Entry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    month: date
    status: Literal['pending', 'done'] = 'pending'
    batch_id: str | None = None
    requested_at: datetime | None = None
    captured_at: datetime | None = None
    points: list[_Point] = Field(default_factory=list, max_length=31)
    error_code: str | None = Field(default=None, max_length=256)

    @model_validator(mode='after')
    def validate_done(self) -> Self:
        if self.month.day != 1:
            raise ValueError('invalid month')
        if self.status == 'done':
            if (not self.batch_id or not self.points or self.requested_at is None or self.captured_at is None
                    or self.requested_at.utcoffset() is None or self.captured_at.utcoffset() is None
                    or self.captured_at < self.requested_at or self.error_code is not None):
                raise ValueError('invalid done')
            if (len({p.observed_at for p in self.points}) != len(self.points)
                    or any(p.observed_at.replace(day=1) != self.month or p.released_at > self.captured_at for p in self.points)):
                raise ValueError('invalid evidence')
        elif self.batch_id is not None or self.requested_at is not None or self.captured_at is not None or self.points:
            raise ValueError('invalid pending')
        return self


def _shift(month: date, count: int) -> date:
    year, offset = divmod(month.year * 12 + month.month - 1 + count, 12)
    return date(year, offset + 1, 1)


def _end(month: date) -> date:
    return month.replace(day=monthrange(month.year, month.month)[1])


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal['1.0'] = '1.0'
    catalog_version: str
    catalog_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    as_of: datetime
    start: date
    end: date
    history_years: int = Field(ge=1, le=30, strict=True)
    entries: list[_Entry] = Field(max_length=360)

    @model_validator(mode='after')
    def validate_window(self) -> Self:
        if (self.as_of.utcoffset() is None or self.end != _end(self.end)
                or self.end >= self.as_of.astimezone(CHINA_TIME).date().replace(day=1)
                or self.start != _shift(self.end, -12 * self.history_years + 1)
                or len(self.entries) != 12 * self.history_years
                or {e.month for e in self.entries} != {_shift(self.start, i) for i in range(12 * self.history_years)}
                or any(e.requested_at is not None and e.requested_at < self.as_of for e in self.entries)):
            raise ValueError('invalid checkpoint')
        return self


def _load(cursors: CursorStore) -> _Checkpoint | None:
    raw = cursors.load(CURSOR_KEY)
    try:
        return None if raw is None else _Checkpoint.model_validate(raw)
    except ValueError:
        raise ValueError('GOVBOND_HISTORY_CHECKPOINT_INVALID') from None


class GovbondHistoryJob:
    def __init__(self, *, catalog: MacroSeriesCatalog, source: MacroSourceDefinition,
                 transport: GovbondTransport | None, cursors: CursorStore,
                 repository: GovbondHistoryRepository, lock: JobLock,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.catalog, self.source, self.transport = catalog, source, transport
        self.cursors, self.repository, self.lock = cursors, repository, lock
        self.clock = clock or (lambda: datetime.now(UTC))
        self.series = catalog.require(SERIES_ID)
        self.source_hash = fingerprint(source.model_dump(mode='json', by_alias=True))

    def _save(self, state: _Checkpoint) -> None:
        # 每次保存前重复完整校验，防止进程内赋值绕过 pydantic。
        checked = _Checkpoint.model_validate(state.model_dump(mode='json'))
        self.cursors.save(CURSOR_KEY, checked.model_dump(mode='json'))

    def _verify_done(self, state: _Checkpoint) -> None:
        for entry in state.entries:
            if entry.status != 'done':
                continue
            try:
                self.repository.verify(self.series, self.source,
                    MacroFetchWindow(entry.month, _end(entry.month), entry.requested_at),
                    entry.captured_at, entry.batch_id, [p.model_dump(mode='json') for p in entry.points])
            except ValueError:
                entry.status, entry.batch_id = 'pending', None
                entry.requested_at = entry.captured_at = None
                entry.points = []
                entry.error_code = 'GOVBOND_HISTORY_EVIDENCE_INVALID'

    def _report(self, state: _Checkpoint, *, requests: int = 0, rows: int = 0) -> dict[str, object]:
        done = [e for e in state.entries if e.status == 'done']
        return {'status': 'completed' if len(done) == len(state.entries) else 'partial',
            'requestCount': requests, 'rowCount': rows, 'start': state.start.isoformat(),
            'end': state.end.isoformat(), 'asOf': state.as_of.isoformat(),
            'catalogVersion': state.catalog_version, 'catalogHash': state.catalog_hash,
            'sourceHash': state.source_hash, 'completedMonths': len(done),
            'pendingMonths': len(state.entries) - len(done), 'confirmedObservations': sum(len(e.points) for e in done),
            'failures': [{'month': e.month.strftime('%Y-%m'), 'code': _public_error(e.error_code)}
                         for e in state.entries if e.error_code is not None]}

    def status(self) -> dict[str, object]:
        state = _load(self.cursors)
        if state is None:
            return {'status': 'not_started', 'requestCount': 0, 'rowCount': 0}
        self._identity(state)
        self._verify_done(state)
        return self._report(state)

    def _identity(self, state: _Checkpoint) -> None:
        if (state.catalog_version != self.catalog.catalog_version
                or not catalog_hashes_match(state.catalog_hash, self.catalog.catalog_hash)
                or state.source_hash != self.source_hash):
            raise ValueError('GOVBOND_HISTORY_CONFIG_MISMATCH')

    def run(self, *, as_of: datetime, history_years: int, end: date | None,
            request_limit: int) -> dict[str, object]:
        if (not isinstance(as_of, datetime) or as_of.utcoffset() is None
                or type(history_years) is not int or not 1 <= history_years <= 30
                or type(request_limit) is not int or not 3 <= request_limit <= 100
                or (end is not None and (type(end) is not date or end != _end(end)
                    or end >= as_of.astimezone(CHINA_TIME).date().replace(day=1)))
                or self.source.params != {'maxPages': '3'}):
            raise ValueError('GOVBOND_HISTORY_CONFIG_INVALID')
        key = data_publication_lock_key()
        if not self.lock.try_acquire(key):
            return {'status': 'locked', 'requestCount': 0, 'rowCount': 0}
        try:
            state = _load(self.cursors)
            if state is None:
                end = end or _end(_shift(as_of.astimezone(CHINA_TIME).date(), -1))
                start = _shift(end, -12 * history_years + 1)
                state = _Checkpoint(catalog_version=self.catalog.catalog_version,
                    catalog_hash=self.catalog.catalog_hash, source_hash=self.source_hash,
                    as_of=as_of, start=start, end=end, history_years=history_years,
                    entries=[_Entry(month=_shift(end, -i)) for i in range(12 * history_years)])
            self._identity(state)
            if (state.history_years != history_years or (end is not None and state.end != end)
                    or as_of < state.as_of):
                raise ValueError('GOVBOND_HISTORY_CONFIG_MISMATCH')
            self._verify_done(state)
            self._save(state)
            run_id = self.repository.start({'catalogVersion': state.catalog_version,
                'catalogHash': state.catalog_hash, 'sourceHash': state.source_hash, 'asOf': as_of.isoformat()})
            requests = rows = 0
            for entry in list(state.entries):
                if entry.status == 'done':
                    continue
                if request_limit - requests < 3:
                    break
                requested = self.clock()
                if requested.utcoffset() is None or requested < as_of:
                    raise ValueError('GOVBOND_HISTORY_CLOCK_INVALID')
                window = MacroFetchWindow(entry.month, _end(entry.month), requested)
                def record_page(raw: dict[str, object], count: int) -> None:
                    self.repository.record_request(self.series, self.source, window,
                        ProviderFetchResult(provider='chinamoney', status='failed', observations=(),
                            raw_payload=raw, request_count=count), self.clock(), run_id, progress=True)
                result = fetch_govbond_history(self.transport, self.series, self.source, window, on_page=record_page)
                requests += result.request_count
                captured = self.clock()
                if captured.utcoffset() is None or captured < requested:
                    raise ValueError('GOVBOND_HISTORY_CLOCK_INVALID')
                batch_id = self.repository.record_request(self.series, self.source, window,
                    result, captured, run_id)
                entry.error_code = 'GOVBOND_HISTORY_EMPTY_MONTH' if result.status == 'no_change' else None
                if result.status == 'failed':
                    entry.error_code = _public_error(result.error.code) if result.error else 'GOVBOND_HISTORY_FETCH_FAILED'
                elif result.status == 'success':
                    try:
                        persisted = self.repository.observations.persist(self.series, result, captured, batch_id=batch_id)
                        rows += persisted.inserted
                        if persisted.rejected:
                            raise ValueError('GOVBOND_HISTORY_EVIDENCE_INVALID')
                        points = self.repository.confirm(self.series, self.source, window, result, captured, batch_id)
                        entry.points = [_Point.model_validate(p) for p in points]
                        entry.batch_id, entry.requested_at, entry.captured_at = batch_id, requested, captured
                        entry.status, entry.error_code = 'done', None
                    except ValueError:
                        entry.error_code = 'GOVBOND_HISTORY_EVIDENCE_INVALID'
                if entry.status != 'done':
                    state.entries.remove(entry)
                    state.entries.append(entry)
                self._save(state)
            report = self._report(state, requests=requests, rows=rows)
            self.repository.finish(run_id, report)
            return report
        finally:
            self.lock.release(key)
