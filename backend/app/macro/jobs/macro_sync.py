from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from app.connectors.macro.contracts import MacroFetchWindow, ProviderError, ProviderFetchResult
from app.connectors.macro.registry import MacroProviderRegistry
from app.jobs.locks import JobLock, macro_sync_lock_key, data_publication_lock_key
from app.macro.jobs.macro_series_state import MacroSeriesStateView
from app.macro.jobs.macro_sync_history import (
    MacroSyncHistory, MacroSyncStatus, macro_safe_error_code, macro_sync_business_key,
)
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, MacroSyncPlanner, MacroWorkItem, state_is_future
from app.macro.jobs.macro_sync_repository import MacroPersistResult
from app.macro.catalog import MacroSeriesCatalog, MacroSeriesDefinition, MacroSourceDefinition
from app.macro.freshness import observation_expired
from app.marts.contracts import QualityResult


MacroSeriesStatus = Literal["fresh", "fallback", "stale", "missing"]


@dataclass(frozen=True, slots=True)
class MacroPublishReport:
    status: Literal["completed", "blocked"]
    version: str | None
    products: tuple[str, ...]
    quality: tuple[QualityResult, ...]
    promoted_products: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


class MacroPublisher(Protocol):
    def publish(
        self, run_id: str, as_of: datetime, catalog: MacroSeriesCatalog,
        statuses: tuple[MacroSeriesOutcome, ...],
    ) -> MacroPublishReport: ...


class MacroObservationStore(Protocol):
    def audit_request(
        self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
        window: MacroFetchWindow, result: ProviderFetchResult,
        captured_at: datetime, run_id: str,
    ) -> str: ...

    def latest_visible(
        self, series: MacroSeriesDefinition, as_of: datetime, end: date,
        *, provider: str | None = None,
    ) -> tuple[str, date, datetime] | None: ...

    def persist(
        self, series: MacroSeriesDefinition, result: ProviderFetchResult,
        captured_at: datetime, *, batch_id: str | None = None,
    ) -> MacroPersistResult: ...


class MacroStateStore(Protocol):
    def get_all(self) -> tuple[MacroSeriesStateView, ...]: ...

    def mark_success(
        self, series_id: str, *, checked_at: datetime, active_source: str,
        latest_observed_at: date | None, latest_released_at: datetime | None,
        fallback_used: bool, history_incomplete: bool = False,
    ) -> None: ...

    def mark_stale(self, series_id: str, *, checked_at: datetime, error: ProviderError) -> None: ...


@dataclass(frozen=True, slots=True)
class MacroSourceFailure:
    provider: str
    error: ProviderError


@dataclass(frozen=True, slots=True)
class MacroSeriesOutcome:
    series_id: str
    status: MacroSeriesStatus
    actual_source: str | None
    request_count: int
    row_count: int
    rejected_count: int
    source_failures: tuple[MacroSourceFailure, ...]
    fallback_used: bool = False
    missing_windows: tuple[MacroFetchWindow, ...] = ()
    deferred_windows: tuple[MacroFetchWindow, ...] = ()
    stale_reason: str | None = None
    recovered_sources: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[ProviderError, ...]:
        return tuple(failure.error for failure in self.source_failures)


@dataclass(frozen=True, slots=True)
class MacroSyncReport:
    status: MacroSyncStatus
    job_run_id: str
    business_key: str
    attempt: int
    as_of: datetime
    catalog_version: str
    outcomes: tuple[MacroSeriesOutcome, ...] = ()
    publication: MacroPublishReport | None = None
    error: ProviderError | None = None
    publication_as_of: datetime | None = None

    @property
    def planned(self) -> int:
        return len(self.outcomes)

    @property
    def completed(self) -> int:
        return sum(item.status in ("fresh", "fallback") for item in self.outcomes)

    @property
    def fallback(self) -> int:
        return sum(item.fallback_used for item in self.outcomes)

    @property
    def stale(self) -> int:
        return sum(item.status == "stale" for item in self.outcomes)

    @property
    def missing(self) -> int:
        return sum(item.status == "missing" for item in self.outcomes)

    @property
    def request_count(self) -> int:
        return sum(item.request_count for item in self.outcomes)

    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.outcomes)

    @property
    def rejected_count(self) -> int:
        return sum(item.rejected_count for item in self.outcomes)

    @property
    def publication_version(self) -> str | None:
        return self.publication.version if self.publication is not None and not self.publication.blocked else None

    @property
    def products(self) -> tuple[str, ...]:
        return self.publication.products if self.publication is not None and not self.publication.blocked else ()

    @property
    def failures(self) -> tuple[ProviderError, ...]:
        series_failures = tuple(failure for item in self.outcomes for failure in item.failures)
        return series_failures + ((self.error,) if self.error is not None else ())

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "jobRunId": self.job_run_id,
            "businessKey": self.business_key,
            "attempt": self.attempt,
            "asOf": self.as_of.isoformat(),
            "publicationAsOf": self.publication_as_of.isoformat() if self.publication_as_of is not None else None,
            "catalogVersion": self.catalog_version,
            "planned": self.planned,
            "completed": self.completed,
            "fallback": self.fallback,
            "stale": self.stale,
            "missing": self.missing,
            "requestCount": self.request_count,
            "rowCount": self.row_count,
            "rejectedCount": self.rejected_count,
            "publicationVersion": self.publication_version,
            "products": list(self.products),
            "failures": [
                {"code": macro_safe_error_code(error.code), "retryable": error.retryable}
                for error in self.failures
            ],
            "outcomes": [
                {
                    "seriesId": item.series_id,
                    "status": item.status,
                    "actualSource": item.actual_source,
                    "fallbackUsed": item.fallback_used,
                    "requestCount": item.request_count,
                    "rowCount": item.row_count,
                    "rejectedCount": item.rejected_count,
                }
                for item in self.outcomes
            ],
        }


class MacroSyncJob:
    def __init__(
        self, *, catalog: MacroSeriesCatalog, registry: MacroProviderRegistry,
        observation_repository: MacroObservationStore,
        state_repository: MacroStateStore,
        history: MacroSyncHistory, lock: JobLock,
        publisher: MacroPublisher, timezone: str,
        clock: Callable[[], datetime] | None = None,
        before_work: Callable[[MacroSyncOptions], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._observations = observation_repository
        self._states = state_repository
        self._history = history
        self._lock = lock
        self._publisher = publisher
        self._timezone = timezone
        self._planner = MacroSyncPlanner(catalog, timezone=timezone)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._before_work = before_work

    def preflight(self, options: MacroSyncOptions) -> bool:
        """在发布锁内验证来源/历史合同，不创建审计、不采集、不发布。"""
        if options.catalog_version != self._catalog.catalog_version or options.catalog_hash != self._catalog.catalog_hash:
            raise ValueError('MACRO_CATALOG_VERSION_HASH_MISMATCH')
        key = data_publication_lock_key()
        if not self._lock.try_acquire(key):
            return False
        try:
            if self._before_work is not None:
                self._before_work(options)
            return True
        finally:
            self._lock.release(key)

    def run(self, options: MacroSyncOptions, *, on_completed: Callable[[MacroSyncReport], MacroSyncReport] | None = None) -> MacroSyncReport:
        if options.catalog_version != self._catalog.catalog_version or options.catalog_hash != self._catalog.catalog_hash:
            raise ValueError("MACRO_CATALOG_VERSION_HASH_MISMATCH")
        business_key = macro_sync_business_key(options.as_of, options.catalog_version, self._timezone)
        completed, attempt = self._history.inspect(
            business_key, options.catalog_version, options.catalog_hash,
        )
        if completed and not options.force:
            return self._terminal(options, business_key, attempt, "skipped")
        lock_key = macro_sync_lock_key(business_key)
        if not self._lock.try_acquire(lock_key):
            return self._terminal(options, business_key, attempt, "locked")
        try:
            publication_key = data_publication_lock_key()
            if not self._lock.try_acquire(publication_key):
                return self._terminal(options, business_key, attempt, "locked")
            try:
                completed, attempt = self._history.inspect(
                    business_key, options.catalog_version, options.catalog_hash,
                )
                if completed and not options.force:
                    return self._terminal(options, business_key, attempt, "skipped")
                if self._before_work is not None:
                    self._before_work(options)
                report = self._run_locked(options, business_key, attempt)
                return on_completed(report) if on_completed is not None else report
            finally:
                self._lock.release(publication_key)
        finally:
            self._lock.release(lock_key)

    def _terminal(
        self, options: MacroSyncOptions, business_key: str, attempt: int,
        status: Literal["skipped", "locked"],
    ) -> MacroSyncReport:
        run_id = self._history.record_terminal(
            business_key, options.as_of, options.catalog_version,
            options.catalog_hash, options.trigger, attempt, status,
        )
        return MacroSyncReport(
            status, run_id, business_key, attempt, options.as_of,
            options.catalog_version,
        )

    def _run_locked(
        self, options: MacroSyncOptions, business_key: str, attempt: int,
    ) -> MacroSyncReport:
        run_id = self._history.start(
            business_key, options.as_of, options.catalog_version,
            options.catalog_hash, options.trigger, attempt,
        )
        outcomes: list[MacroSeriesOutcome] = []
        publication: MacroPublishReport | None = None
        run_error: ProviderError | None = None
        publishing = False
        carried_gaps: dict[str, tuple[tuple[date, date], ...]] = {}
        has_gap_snapshot = False
        status: MacroSyncStatus = "failed"
        publication_as_of: datetime | None = None
        try:
            started_at = self._clock()
            live = options.mode == "live"
            planning_options = replace(options, as_of=max(options.as_of, started_at)) if live else options
            carried_gaps = self._history.unresolved_gaps(
                options.catalog_version, options.catalog_hash,
            )
            has_gap_snapshot = True
            states = {item.series_id: item for item in self._states.get_all()}
            end = self._planner.end_date(planning_options.as_of)
            # 限定同步在锁内已验证历史证据；只用真实可见core形成临时规划状态。
            # 默认全指标同步不改变原有规划语义。
            for series_id in options.series_ids:
                definition = self._catalog.require(series_id)
                visible = self._observations.latest_visible(definition, planning_options.as_of, end)
                if visible is not None:
                    states[series_id] = _visible_state(series_id, options.catalog_version,
                        states.get(series_id), visible)
            planned = tuple(self._planner.plan(planning_options, states))
            if live and hasattr(self._history, 'record_collection_scope'):
                self._history.record_collection_scope(run_id, [work.series.id for work in planned if work.series.sources])
            for work in planned:
                current = states.get(work.series.id)
                preserve_current = state_is_future(current, planning_options.as_of, end)
                prior = current if not preserve_current else None
                try:
                    visible = self._observations.latest_visible(
                        work.series, planning_options.as_of, work.window.end,
                    )
                    priorities = {source.provider: source.priority for source in work.series.sources}
                    if visible is not None and (
                        prior is None or prior.latest_observed_at is None
                        or visible[1] > prior.latest_observed_at
                        or (
                            visible[1] == prior.latest_observed_at
                            and priorities.get(visible[0], float("inf"))
                            < priorities.get(prior.active_source, float("inf"))
                        )
                    ):
                        prior = _visible_state(work.series.id, options.catalog_version, current, visible)
                    outcome = self._sync_one(
                        work, prior, carried_gaps.get(work.series.id, ()),
                        run_id=run_id,
                        visible_source=visible[0] if visible is not None else None,
                        write_state=live and not preserve_current, live=live,
                        offline_available=(visible is not None and visible[0] not in priorities
                                           and prior is not None and prior.active_source == visible[0]
                                           and prior.latest_observed_at == visible[1]),
                        require_online_success=bool(options.series_ids),
                    )
                except Exception:
                    error = _safe_error("MACRO_SERIES_ERROR")
                    outcome = MacroSeriesOutcome(
                        work.series.id,
                        "stale" if _has_value(prior) else "missing",
                        prior.active_source if _has_value(prior) else None,
                        0, 0, 0, (MacroSourceFailure("unavailable", error),), False,
                        tuple(
                            MacroFetchWindow(start, end, options.as_of)
                            for start, end in carried_gaps.get(work.series.id, ())
                            if end <= work.window.end
                        ),
                        tuple(
                            MacroFetchWindow(start, end, options.as_of)
                            for start, end in carried_gaps.get(work.series.id, ())
                            if end > work.window.end
                        ),
                    )
                    # 无实际请求证据的内部异常只进入运行报告，不伪造采集时间。
                outcomes.append(outcome)
                for failure in outcome.source_failures:
                    self._history.record_failure(
                        run_id, work.series.id,
                        failure.provider, failure.error.code,
                        failure.error.code, failure.error.retryable,
                    )
                if live and not preserve_current:
                    for provider in outcome.recovered_sources:
                        self._history.resolve_failures(work.series.id, provider,
                            planning_options.as_of, self._clock())
            publication_as_of = max(planning_options.as_of, self._clock()) if live else options.as_of
            if options.publish:
                publishing = True
                publication = self._publisher.publish(
                    run_id, publication_as_of, self._catalog, tuple(outcomes),
                )
                if publication.blocked:
                    publication = replace(publication, version=None, products=())
            status = "failed" if publication is not None and publication.blocked else (
                "partial" if any(item.status in ("stale", "missing") for item in outcomes)
                else "completed"
            )
            if publication is not None and publication.blocked:
                run_error = _safe_error("MACRO_PUBLICATION_BLOCKED")
        except Exception:
            run_error = _safe_error(
                "MACRO_PUBLICATION_FAILED" if publishing else "MACRO_SYNC_FAILED"
            )
            status = "failed"
        report = MacroSyncReport(
            status, run_id, business_key, attempt, options.as_of,
            options.catalog_version, tuple(outcomes), publication, run_error,
            publication_as_of,
        )
        details: dict[str, object] = {
            "planned": report.planned,
            "seriesIds": list(options.series_ids),
            "publicationRequested": options.publish,
            "completed": report.completed,
            "fallback": report.fallback,
            "stale": report.stale,
            "missing": report.missing,
            "requestCount": report.request_count,
            "rowCount": report.row_count,
            "rejectedCount": report.rejected_count,
            "publicationVersion": report.publication_version,
            "publicationAsOf": publication_as_of.isoformat() if publication_as_of is not None else None,
            "products": list(report.products),
            "actualSources": {
                item.series_id: item.actual_source
                for item in report.outcomes if item.actual_source is not None
            },
            "errorCode": run_error.code if run_error is not None else None,
        }
        if has_gap_snapshot:
            unresolved: dict[str, list[dict[str, str]]] = {
                series_id: [
                    {"start": start.isoformat(), "end": end.isoformat()}
                    for start, end in windows
                ]
                for series_id, windows in carried_gaps.items()
            }
            for item in report.outcomes:
                remaining = (*item.missing_windows, *item.deferred_windows)
                if remaining:
                    unresolved[item.series_id] = [
                        {"start": window.start.isoformat(), "end": window.end.isoformat()}
                        for window in remaining
                    ]
                else:
                    unresolved.pop(item.series_id, None)
            details["unresolvedGaps"] = unresolved
        self._history.finish(run_id, status, details)
        return report

    def _sync_one(
        self, work: MacroWorkItem, prior: MacroSeriesStateView | None,
        carried_gaps: tuple[tuple[date, date], ...],
        *, run_id: str, visible_source: str | None = None,
        write_state: bool = True, live: bool = False, offline_available: bool = False,
        require_online_success: bool = False,
    ) -> MacroSeriesOutcome:
        series = work.series
        sources = sorted(series.sources, key=lambda item: item.priority)
        source_priorities = {source.provider: source.priority for source in sources}
        failures: list[MacroSourceFailure] = []
        recovered: set[str] = set()
        requests = rows = rejected = 0
        chosen_source: str | None = None
        incumbent_source = prior.active_source if _has_value(prior) else None
        fallback_used = False
        latest_observed = prior.latest_observed_at if prior is not None else None
        latest_released = prior.latest_released_at if prior is not None else None
        if offline_available and prior is not None:
            chosen_source = prior.active_source
        visibility_cutoff = work.window.as_of
        online_success = False
        collection_success = False
        collection_failures: dict[MacroFetchWindow, ProviderError] = {}
        pending = ((work.window, False),) + tuple(
            (MacroFetchWindow(start, end, work.window.as_of), True)
            for start, end in carried_gaps if start <= end and end <= work.window.end
        )
        deferred_windows = tuple(
            MacroFetchWindow(start, end, work.window.as_of)
            for start, end in carried_gaps if start <= end and end > work.window.end
        )
        for index, source in enumerate(sources):
            next_pending: list[tuple[MacroFetchWindow, bool]] = []
            for window, was_explicit_gap in pending:
                fetched = None
                batch_id = None
                attempt_requests = 0
                attempt_success = False
                attempt_error: ProviderError | None = None
                try:
                    provider = self._registry.require(source.provider)
                    fetched = provider.fetch(series, source, window)
                    attempt_requests = fetched.request_count
                    if fetched.provider != source.provider:
                        raise ValueError("provider mismatch")
                    requests += fetched.request_count
                    if fetched.request_count > 0:
                        batch_id = self._observations.audit_request(
                            series, source, window, fetched, self._clock(), run_id,
                        )
                    if fetched.status in ("failed", "unavailable"):
                        failures.append(MacroSourceFailure(
                            source.provider,
                            _safe_error(fetched.error.code if fetched.error else "MACRO_SOURCE_FAILED"),
                        ))
                        attempt_error = failures[-1].error
                        next_pending.append((window, was_explicit_gap))
                        continue
                    if fetched.status == "no_change":
                        declared_gaps = _explicit_gaps(fetched.raw_payload, window)
                        if require_online_success:
                            attempt_error = _safe_error('MACRO_SOURCE_FAILED')
                            failures.append(MacroSourceFailure(source.provider, attempt_error))
                            next_pending.append((window, was_explicit_gap))
                            continue
                        attempt_success = True
                        if chosen_source is None and prior is not None:
                            chosen_source = prior.active_source
                        if was_explicit_gap:
                            next_pending.append((window, True))
                        elif declared_gaps:
                            next_pending.extend((gap, True) for gap in declared_gaps)
                        elif visible_source is None:
                            next_pending.append((window, False))
                        elif (prior is not None and prior.active_source == source.provider
                              and visible_source == source.provider and _has_value(prior)):
                            recovered.add(source.provider)
                        continue
                    if fetched.status != "success":
                        raise ValueError("invalid provider status")
                    declared_gaps = _explicit_gaps(fetched.raw_payload, window)
                    captured_at = self._clock()
                    if live:
                        visibility_cutoff = max(visibility_cutoff, captured_at)
                    in_window = tuple(
                        row for row in fetched.observations
                        if window.start <= row.observed_at <= window.end
                    )
                    visible_rows = []
                    deferred_rows = []
                    for row in in_window:
                        target_rows = (
                            visible_rows
                            if (row.released_at or captured_at) <= visibility_cutoff
                            else deferred_rows
                        )
                        target_rows.append(row)
                    persisted = None
                    if visible_rows:
                        persisted = self._observations.persist(
                            series, replace(fetched, observations=tuple(visible_rows)), captured_at,
                            batch_id=batch_id,
                        )
                        rows += persisted.inserted
                        rejected += persisted.rejected
                    if deferred_rows:
                        deferred = self._observations.persist(
                            series, replace(fetched, observations=tuple(deferred_rows)), captured_at,
                            batch_id=batch_id,
                        )
                        rows += deferred.inserted
                        rejected += deferred.rejected
                    if persisted is None:
                        failures.append(MacroSourceFailure(
                            source.provider, _safe_error("MACRO_ROWS_REJECTED"),
                        ))
                        attempt_error = failures[-1].error
                        next_pending.append((window, was_explicit_gap))
                        if not was_explicit_gap:
                            next_pending.extend((gap, True) for gap in declared_gaps)
                        continue
                    if persisted.received == 0 or persisted.inserted + persisted.unchanged == 0:
                        failures.append(MacroSourceFailure(
                            source.provider, _safe_error("MACRO_ROWS_REJECTED"),
                        ))
                        attempt_error = failures[-1].error
                        next_pending.append((window, was_explicit_gap))
                        if not was_explicit_gap:
                            next_pending.extend((gap, True) for gap in declared_gaps)
                        continue
                    if persisted.rejected:
                        attempt_error = _safe_error('MACRO_ROWS_REJECTED')
                    if (not declared_gaps and persisted.rejected == 0
                            and persisted.latest_observed_at is not None
                            and not observation_expired(series, persisted.latest_observed_at, work.window.end)
                            and (persisted.latest_released_at is None
                                 or persisted.latest_released_at <= visibility_cutoff)):
                        online_success = True
                    if persisted.latest_released_at is not None and persisted.latest_released_at > visibility_cutoff:
                        next_pending.append((window, was_explicit_gap))
                        continue
                    source_visible = self._observations.latest_visible(
                        series, visibility_cutoff, window.end, provider=source.provider,
                    )
                    if source_visible is None:
                        failures.append(MacroSourceFailure(
                            source.provider, _safe_error("MACRO_VALUE_INVALIDATED"),
                        ))
                        attempt_error = failures[-1].error
                        # An already valid fallback can still serve this series,
                        # but it is never evidence that the primary recovered.
                        if self._observations.latest_visible(series, visibility_cutoff, window.end) is None:
                            next_pending.append((window, was_explicit_gap))
                            continue
                    attempt_success = source_visible is not None and persisted.rejected == 0
                    if (persisted.rejected == 0 and not declared_gaps
                            and (not was_explicit_gap or "missingWindows" in fetched.raw_payload)
                            and source_visible is not None
                            and not observation_expired(series, source_visible[1], work.window.end)):
                        recovered.add(source.provider)
                    if index > 0:
                        fallback_used = True
                    if persisted.latest_observed_at is not None and (
                        latest_observed is None
                        or persisted.latest_observed_at > latest_observed
                        or (
                            persisted.latest_observed_at == latest_observed
                            and source.priority < source_priorities.get(
                                chosen_source or incumbent_source, float("inf"),
                            )
                        )
                    ):
                        latest_observed = persisted.latest_observed_at
                        latest_released = persisted.latest_released_at
                        chosen_source = source.provider
                    elif chosen_source is None:
                        chosen_source = incumbent_source
                    if was_explicit_gap and "missingWindows" not in fetched.raw_payload:
                        next_pending.append((window, True))
                    else:
                        next_pending.extend((gap, True) for gap in declared_gaps)
                except Exception as exc:
                    if isinstance(exc, ProviderError):
                        if fetched is None:
                            attempt_requests = exc.request_count
                            requests += exc.request_count
                            if exc.request_count > 0:
                                safe_error = _safe_error(exc.code, exc.retryable)
                                self._observations.audit_request(series, source, window,
                                    ProviderFetchResult(provider=source.provider, status="failed",
                                        observations=(), raw_payload={"errorCode": safe_error.code},
                                        request_count=exc.request_count, error=safe_error),
                                    self._clock(), run_id)
                        failures.append(MacroSourceFailure(
                            source.provider, _safe_error(exc.code, exc.retryable),
                        ))
                    else:
                        failures.append(MacroSourceFailure(
                            source.provider, _safe_error("MACRO_SOURCE_ERROR"),
                        ))
                    attempt_error = failures[-1].error
                    next_pending.append((window, was_explicit_gap))
                finally:
                    # 每个窗口单独保留实际请求结果；备用源的真实成功才能恢复。
                    # 预算终止不是供应商失败，历史完整性继续由 outcome/gaps 表达。
                    if attempt_requests > 0:
                        if attempt_error is not None and not attempt_error.code.endswith('_REQUEST_BUDGET_EXHAUSTED'):
                            collection_failures[window] = attempt_error
                        elif attempt_success:
                            collection_success = True
                            collection_failures.pop(window, None)
            if not next_pending:
                pending = ()
                break
            pending = tuple(next_pending)
            if index == len(sources) - 1:
                break
        if not sources and chosen_source is None:
            failures.append(MacroSourceFailure("unavailable", _safe_error("MACRO_NO_SOURCE")))
        missing_windows = tuple(window for window, explicit in pending if explicit) if pending else ()
        if missing_windows:
            failures.append(MacroSourceFailure(
                sources[-1].provider if sources else "unavailable", _safe_error("MACRO_HISTORY_GAP"),
            ))
        # State and persist counters are not evidence that a revision is still
        # valid. Re-read the same as-of selector used by rebuild and snapshots.
        visible = self._observations.latest_visible(series, visibility_cutoff, work.window.end)
        if visible is not None and chosen_source is not None:
            chosen_source, latest_observed, latest_released = visible
        else:
            recovered.clear()
        # 已被主源替换的离线值不继续标记回退；本轮历史回填实际使用的在线回退仍保留。
        fallback_used = fallback_used or (chosen_source is not None and chosen_source not in source_priorities)
        fresh = (
            visible is not None and chosen_source is not None and latest_observed is not None
            and latest_observed <= work.window.end
            and (latest_released is None or latest_released <= visibility_cutoff)
            and not observation_expired(series, latest_observed, work.window.end)
        )
        # 采集状态只反映实际执行，日期新鲜度仍留在原业务 outcome 中。
        # 备用源实际成功可恢复；仅旧值或零请求 no_change 不能覆盖失败。
        if write_state and sources:
            if collection_success and not collection_failures and chosen_source is not None:
                fallback_used = fallback_used or source_priorities.get(chosen_source, float('inf')) > sources[0].priority
                self._states.mark_success(series.id, checked_at=max(work.window.as_of, self._clock()),
                    active_source=chosen_source, latest_observed_at=latest_observed,
                    latest_released_at=latest_released, fallback_used=fallback_used,
                    history_incomplete=bool(missing_windows or deferred_windows))
            elif collection_failures:
                self._states.mark_stale(series.id, checked_at=max(work.window.as_of, self._clock()),
                    error=next(reversed(collection_failures.values())))
        if fresh and chosen_source is not None and not missing_windows and (
                not require_online_success or (online_success and not failures)):
            if sources and source_priorities.get(chosen_source, float("inf")) > sources[0].priority:
                fallback_used = True
            if not sources and offline_available:
                recovered.add("unavailable")
            if require_online_success and online_success:
                recovered.add('unavailable')
            return MacroSeriesOutcome(
                series.id, "fallback" if fallback_used else "fresh", chosen_source,
                requests, rows, rejected, tuple(failures), fallback_used,
                deferred_windows=deferred_windows,
                recovered_sources=tuple(sorted(recovered - {failure.provider for failure in failures}))
                    if not deferred_windows else (),
            )
        last_failure = failures[-1] if failures else MacroSourceFailure(
            chosen_source or "unavailable", _safe_error("MACRO_VALUE_EXPIRED"),
        )
        error = last_failure.error
        return MacroSeriesOutcome(
            series.id, "stale" if latest_observed is not None else "missing",
            chosen_source or (prior.active_source if _has_value(prior) else None),
            requests, rows, rejected, tuple(failures or [last_failure]), fallback_used,
            missing_windows, deferred_windows, stale_reason=error.code,
        )


def _has_value(state: MacroSeriesStateView | None) -> bool:
    return state is not None and state.latest_observed_at is not None


def _visible_state(
    series_id: str, catalog_version: str, current: MacroSeriesStateView | None,
    visible: tuple[str, date, datetime],
) -> MacroSeriesStateView:
    if current is None:
        return MacroSeriesStateView(
            series_id=series_id,
            catalog_version=catalog_version,
            last_checked_at=None,
            last_success_at=None,
            latest_observed_at=visible[1],
            latest_released_at=visible[2],
            active_source=visible[0],
            fallback_used=False,
            stale=False,
            stale_reason=None,
            consecutive_failures=0,
        )
    return replace(
        current, last_checked_at=None, last_success_at=None,
        latest_observed_at=visible[1], latest_released_at=visible[2],
        active_source=visible[0], fallback_used=False,
        stale=False, stale_reason=None, consecutive_failures=0,
    )


def _safe_error(code: str, retryable: bool = False) -> ProviderError:
    safe_code = macro_safe_error_code(code)
    return ProviderError(safe_code, safe_code, retryable=retryable)


def _explicit_gaps(
    raw_payload: Mapping[str, object], requested: MacroFetchWindow,
) -> tuple[MacroFetchWindow, ...]:
    declared = raw_payload.get("missingWindows")
    if declared is None and "missingWindows" not in raw_payload:
        return ()
    if not isinstance(declared, list):
        raise ValueError("invalid missingWindows")
    windows: list[MacroFetchWindow] = []
    for item in declared:
        if not isinstance(item, dict):
            raise ValueError("invalid missingWindows item")
        start_value = item.get("start")
        end_value = item.get("end")
        if not isinstance(start_value, str) or not isinstance(end_value, str):
            raise ValueError("invalid missingWindows bounds")
        start = date.fromisoformat(start_value)
        end = date.fromisoformat(end_value)
        if start < requested.start or end > requested.end or start > end:
            raise ValueError("missingWindows outside requested window")
        windows.append(MacroFetchWindow(start, end, requested.as_of))
    return tuple(windows)
