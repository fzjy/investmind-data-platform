from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from functools import wraps
from threading import Condition, RLock
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import Settings, configured_tushare_token
from app.content.announcement_runtime import announcement_policy, run_announcements
from app.shared.cli import SafeArgumentParser
from app.stocks.universes.cli_selection import resolve_cli_selection
from app.stocks.jobs.daily_sync_runtime import (
    DailySyncRuntime,
    open_daily_sync_runtime,
    redact_sensitive_values,
)
from app.jobs.freshness import FreshnessService
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_scheduler import MacroScheduledCoordinator, MacroWeeklyCronTrigger
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_runtime import open_macro_sync_runtime
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.stocks.jobs.scheduler_coordinator import ScheduledDailySyncCoordinator
from app.jobs.scheduler_logging import SchedulerJsonLogger
from app.stocks.jobs.scheduler_planning import (
    CatchupPlanner,
    ExpectedBusinessDateResolver,
    TradeCalendarUnavailable,
)
from app.jobs.scheduler_state import SchedulerStateRepository
from app.market.jobs.market_scheduler import MarketScheduledCoordinator, MarketDailyCronTrigger, PulseScheduledCoordinator, RegionScheduledCoordinator, EtfScheduledCoordinator, CommodityScheduledCoordinator
from app.jobs.supplier_job_pacing import SupplierJobPacer
from app.stocks.universes.models import UniverseSelection


class SchedulerCoordinator(Protocol):
    def recover_interrupted(self, now: datetime) -> tuple[str, ...]: ...

    def run_scheduled(self, now: datetime) -> object: ...

    def run_startup(self, now: datetime) -> object: ...


class SchedulerState(Protocol):
    def start_instance(
        self,
        scheduler_name: str,
        *,
        now: datetime,
        details: dict[str, object],
    ) -> str: ...

    def touch_instance(
        self,
        instance_id: str,
        *,
        now: datetime,
        next_run_at: datetime | None,
        state: str,
    ) -> object: ...

    def heartbeat(self, instance_id: str) -> object | None: ...

    def stop_instance(self, instance_id: str, *, now: datetime) -> None: ...

    def alert(self, key: str) -> object | None: ...


class SchedulerJobError(RuntimeError):
    """Safe failure exposed to APScheduler and its exception listeners."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(f"scheduler job failed ({error_code})")


def create_scheduler(settings: Settings | None = None) -> BlockingScheduler:
    active_settings = settings or Settings.from_env()
    return BlockingScheduler(timezone=active_settings.sync_timezone)


def configure_scheduler(
    scheduler: BlockingScheduler,
    settings: Settings,
    coordinator: SchedulerCoordinator,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    heartbeat_job: Callable[[], object] | None = None,
    freshness_job: Callable[[], object] | None = None,
    daily_job: Callable[[], object] | None = None,
    startup_job: Callable[[], object] | None = None,
    macro_job: Callable[[], object] | None = None,
    market_job: Callable[[], object] | None = None,
    market_schedule=None,
    pulse_job: Callable[[], object] | None = None,
    pulse_schedule=None,
    region_job: Callable[[], object] | None = None,
    region_schedule=None,
    etf_job: Callable[[], object] | None = None,
    etf_schedule=None,
    commodity_job: Callable[[], object] | None = None,
    commodity_schedule=None,
    announcement_job: Callable[[], object] | None = None,
) -> BlockingScheduler:
    if announcement_job is not None:
        scheduler.add_job(announcement_job, trigger="cron", hour=settings.sync_hour, minute=settings.sync_minute,
                          id="sync-announcements", replace_existing=True, max_instances=1, coalesce=True)
    if settings.routine_commodities_enabled and commodity_job is not None and commodity_schedule is not None:
        scheduler.add_job(commodity_job, trigger=MarketDailyCronTrigger(commodity_schedule),
            id='sync-commodities', replace_existing=True, max_instances=1, coalesce=True)
    if settings.routine_etf_heatmap_enabled and etf_job is not None and etf_schedule is not None:
        scheduler.add_job(etf_job, trigger=MarketDailyCronTrigger(etf_schedule),
            id='sync-etf-heatmap', replace_existing=True, max_instances=1, coalesce=True)
    if settings.routine_region_enabled and region_job is not None and region_schedule is not None:
        scheduler.add_job(region_job, trigger=MarketDailyCronTrigger(region_schedule),
            id='sync-security-region', replace_existing=True, max_instances=1, coalesce=True)
    if settings.routine_market_pulse_enabled and pulse_job is not None and pulse_schedule is not None:
        scheduler.add_job(pulse_job, trigger=MarketDailyCronTrigger(pulse_schedule),
            id='sync-market-pulse', replace_existing=True, max_instances=1, coalesce=True)
    if settings.routine_market_enabled and market_job is not None and market_schedule is not None:
        scheduler.add_job(market_job, trigger=MarketDailyCronTrigger(market_schedule),
            id='sync-market-data', replace_existing=True, max_instances=1, coalesce=True)
    if settings.macro_sync_enabled and macro_job is not None:
        scheduler.add_job(
            macro_job, trigger=MacroWeeklyCronTrigger(
                settings.macro_sync_day_of_week, settings.macro_sync_time,
                settings.sync_timezone,
            ),
            id="sync-macro", replace_existing=True, max_instances=1, coalesce=True,
        )
    scheduler.add_job(
        daily_job or (lambda: coordinator.run_scheduled(clock())),
        trigger="cron",
        hour=settings.sync_hour,
        minute=settings.sync_minute,
        id="sync-daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        heartbeat_job or _noop_job,
        trigger="interval",
        seconds=settings.scheduler_heartbeat_seconds,
        id="scheduler-heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        freshness_job or _noop_job,
        trigger="interval",
        seconds=settings.sync_freshness_interval_seconds,
        id="snapshot-freshness",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=clock(),
        misfire_grace_time=None,
    )
    scheduler.add_job(
        startup_job or (lambda: coordinator.run_startup(clock())),
        trigger="date",
        run_date=clock(),
        id="startup-catchup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )
    return scheduler


class SchedulerRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        state: SchedulerState,
        universe: UniverseSelection,
        coordinator_factory: Callable[[str], SchedulerCoordinator],
        prepare_checkpoint: Callable[[datetime], object],
        freshness_runner: Callable[[datetime], object],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        logger_factory: Callable[[str], SchedulerJsonLogger] = SchedulerJsonLogger,
        macro_coordinator: MacroScheduledCoordinator | None = None,
        market_coordinator=None,
        pulse_coordinator=None,
        region_coordinator=None,
        etf_coordinator=None,
        commodity_coordinator=None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._universe = universe
        self._coordinator_factory = coordinator_factory
        self._prepare_checkpoint = prepare_checkpoint
        self._freshness_runner = freshness_runner
        self._clock = clock
        self._logger_factory = logger_factory
        self._macro_coordinator = macro_coordinator
        self._market_coordinator = market_coordinator
        self._pulse_coordinator = pulse_coordinator
        self._region_coordinator = region_coordinator
        self._etf_coordinator = etf_coordinator
        self._commodity_coordinator = commodity_coordinator
        rpm = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
        self._supplier_pacer = SupplierJobPacer(rpm)
        self._scheduler: BlockingScheduler | None = None
        self._instance_id: str | None = None
        self._logger: SchedulerJsonLogger | None = None
        self._coordinator: SchedulerCoordinator | None = None
        self._degraded_reasons: set[str] = set()
        self._state_write_pending = False
        self._state_transition_lock = RLock()
        self._operations_lock = RLock()
        self._jobs_condition = Condition()
        self._jobs_stopping = False
        self._active_jobs = 0

    def run(self) -> None:
        instance_id = self._state.start_instance(
            "daily-sync",
            now=self._now(),
            details=self._universe.to_payload(),
        )
        self._instance_id = instance_id
        logger = self._logger_factory(instance_id)
        self._logger = logger
        primary_error: BaseException | None = None
        normal_stop = False
        scheduler: BlockingScheduler | None = None
        try:
            self._state.touch_instance(
                instance_id,
                now=self._now(),
                next_run_at=None,
                state="starting",
            )
            coordinator = self._coordinator_factory(instance_id)
            self._coordinator = coordinator
            recovered = coordinator.recover_interrupted(self._now())
            for run_id in recovered:
                logger.emit(
                    "scheduler.interrupted-run.recovered",
                    "warning",
                    job_run_id=run_id,
                )
            checkpoint_ready = self._prepare_checkpoint(self._now())
            if checkpoint_ready is False:
                self._degraded_reasons.add("calendar")
            initial_state = self._operational_state
            scheduler = create_scheduler(self._settings)
            self._scheduler = scheduler
            configure_scheduler(
                scheduler,
                self._settings,
                coordinator,
                clock=self._now,
                announcement_job=(self._guard_job(self._run_announcements, "announcements")
                                  if self._settings.routine_stocks_enabled and announcement_policy()["enabled"] else None),
                heartbeat_job=self._guard_job(self._run_heartbeat, "heartbeat"),
                freshness_job=self._guard_job(self._run_freshness, "freshness"),
                daily_job=self._guard_job(self._run_daily, "daily"),
                startup_job=self._guard_job(self._run_startup_jobs, "catchup"),
                macro_job=(self._guard_job(self._run_macro, "macro") if self._macro_coordinator else None),
                market_job=(self._guard_job(self._run_market, 'market') if self._market_coordinator and self._market_coordinator.catalog.enabled else None),
                market_schedule=(self._market_coordinator.catalog.schedule if self._market_coordinator and self._market_coordinator.catalog else None),
                pulse_job=(self._guard_job(self._run_pulse, 'pulse') if self._pulse_coordinator and self._pulse_coordinator.catalog
                    and self._pulse_coordinator.catalog.enabled and self._pulse_coordinator.catalog.pulse.enabled else None),
                pulse_schedule=(self._pulse_coordinator.catalog.schedule if self._pulse_coordinator and self._pulse_coordinator.catalog else None),
                region_job=(self._guard_job(self._run_region,'region') if self._region_coordinator and self._region_coordinator.catalog
                    and self._region_coordinator.catalog.enabled and self._region_coordinator.catalog.region.enabled else None),
                region_schedule=(self._region_coordinator.catalog.schedule if self._region_coordinator and self._region_coordinator.catalog else None),
                etf_job=(self._guard_job(self._run_etf,'etf') if self._etf_coordinator and self._etf_coordinator.catalog
                    and self._etf_coordinator.catalog.enabled and self._etf_coordinator.catalog.etf_heatmap.enabled else None),
                etf_schedule=(self._etf_coordinator.catalog.schedule if self._etf_coordinator and self._etf_coordinator.catalog else None),
                commodity_job=(self._guard_job(self._run_commodity,'commodities') if self._commodity_coordinator and self._commodity_coordinator.catalog
                    and self._commodity_coordinator.catalog.enabled and self._commodity_coordinator.catalog.commodities.enabled else None),
                commodity_schedule=(self._commodity_coordinator.catalog.schedule if self._commodity_coordinator and self._commodity_coordinator.catalog else None),
            )
            self._state.touch_instance(
                instance_id,
                now=self._now(),
                next_run_at=None,
                state=initial_state,
            )
            logger.emit(
                "scheduler.started",
                "info",
                status=initial_state,
                universe_profile=self._universe.profile,
                universe_version=self._universe.version,
            )
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            normal_stop = True
        except BaseException as error:
            primary_error = error
            try:
                logger.emit(
                    "scheduler.failed",
                    "error",
                    error_code=_error_code(error),
                    message="scheduler runtime failed",
                )
            except BaseException:
                pass
            raise
        finally:
            self._stop(
                scheduler,
                primary_error=primary_error,
                suppress_cleanup_error=normal_stop,
            )

    def _guard_job(
        self, job: Callable[[], object], reason: str,
    ) -> Callable[[], object]:
        @wraps(job)
        def guarded() -> object:
            with self._jobs_condition:
                if self._jobs_stopping:
                    return None
            # Serialize data operations so startup cannot publish an old backlog
            # result after periodic reconciliation. Heartbeats remain independent.
            lock = nullcontext() if reason == "heartbeat" else self._operations_lock
            with lock:
                with self._jobs_condition:
                    if self._jobs_stopping:
                        return None
                    self._active_jobs += 1
                try:
                    try:
                        # catchup外层仅管理生命周期；其内部各供应商组分别节流。
                        paced = self._supplier_pacer.scope() if (self._settings.routine_market_enabled or self._settings.routine_market_pulse_enabled or self._settings.routine_region_enabled or self._settings.routine_etf_heatmap_enabled or self._settings.routine_commodities_enabled) and reason in {'daily','macro','market','pulse','region','etf','commodities'} else nullcontext()
                        with paced:
                            return job()
                    except Exception as error:
                        self._degrade_after_error(reason)
                        safe_error = SchedulerJobError(_error_code(error))
                    # Raise outside the handler: no original traceback or chain
                    # reaches APScheduler's logger.exception or error listeners.
                    raise safe_error from None
                finally:
                    with self._jobs_condition:
                        self._active_jobs -= 1
                        self._jobs_condition.notify_all()

        return guarded

    def _run_daily(self) -> object:
        assert self._logger is not None
        assert self._coordinator is not None
        now = self._now()
        business_date = now.astimezone(ZoneInfo(self._settings.sync_timezone)).date()
        self._logger.emit(
            "scheduler.daily.started",
            "info",
            business_date=business_date,
            universe_profile=self._universe.profile,
            universe_version=self._universe.version,
        )
        try:
            report = self._coordinator.run_scheduled(now)
            calendar_unavailable = self._calendar_alert_is_open()
        except Exception as error:
            self._degrade_after_error("daily")
            self._log_job_failure(
                "scheduler.daily.failed", error, business_date=business_date,
            )
            raise
        if report is None:
            if calendar_unavailable:
                self._set_degraded("calendar", True)
                self._logger.emit(
                    "scheduler.daily.failed",
                    "error",
                    business_date=business_date,
                    error_code="TRADE_CALENDAR_UNAVAILABLE",
                    message="trade calendar unavailable",
                )
            else:
                self._set_degraded("calendar", False)
                self._logger.emit(
                    "scheduler.daily.skipped",
                    "info",
                    business_date=business_date,
                    status="non-trading-day",
                )
            return report
        self._set_degraded("calendar", False)
        status = str(getattr(report, "status", "completed"))
        event = (
            "scheduler.daily.failed"
            if status == "failed"
            else "scheduler.daily.skipped"
            if status in {"locked", "skipped"}
            else "scheduler.daily.completed"
        )
        level = "error" if status == "failed" else "info"
        if status == "failed":
            self._set_degraded("daily", True)
        elif status in {"completed", "partial"}:
            self._set_degraded("daily", False)
        self._logger.emit(
            event,
            level,
            business_date=getattr(report, "business_date", business_date),
            status=status,
        )
        return report

    def _run_startup_jobs(self) -> object:
        primary = None
        report = None
        operations = [self._run_startup, self._run_market_startup, self._run_pulse_startup, self._run_region_startup,
                      self._run_etf_startup, self._run_commodity_startup, self._run_macro_startup]
        if self._settings.routine_stocks_enabled and announcement_policy()["enabled"]:
            operations.append(self._run_announcements)
        for operation in operations:
            try:
                paced = self._supplier_pacer.scope() if (self._settings.routine_market_enabled or self._settings.routine_market_pulse_enabled or self._settings.routine_region_enabled or self._settings.routine_etf_heatmap_enabled or self._settings.routine_commodities_enabled) and operation == self._run_startup else nullcontext()
                with paced:
                    result = operation()
                if operation == self._run_startup:
                    report = result
            except Exception as error:
                if primary is None:
                    primary = error
        if primary is not None:
            raise primary
        return report

    def _run_market_startup(self):
        if self._market_coordinator is not None:
            return self._guard_job(lambda:self._run_market(startup=True), 'market')()

    def _run_pulse_startup(self):
        if self._pulse_coordinator is not None:
            return self._guard_job(lambda:self._run_pulse(startup=True), 'pulse')()

    def _run_region_startup(self):
        if self._region_coordinator is not None:
            return self._guard_job(lambda:self._run_region(startup=True),'region')()

    def _run_etf_startup(self):
        if self._etf_coordinator is not None:
            return self._guard_job(lambda:self._run_etf(startup=True),'etf')()

    def _run_commodity_startup(self):
        if self._commodity_coordinator is not None:
            return self._guard_job(lambda:self._run_commodity(startup=True),'commodities')()

    def _run_announcements(self):
        policy = announcement_policy()
        if not policy["enabled"]:
            return
        result = run_announcements(self._settings, page_budget=policy["maxPagesPerRun"], revisit_days=policy["revisitDays"], publish=True)
        failed = result["status"] == "failed"
        self._set_degraded("announcements", failed)
        self._logger.emit("scheduler.announcements." + result["status"], "error" if failed else "info", status=result["status"])
        if failed:
            raise SchedulerJobError("ANNOUNCEMENT_COLLECTION_FAILED")
        return result

    def _run_commodity(self, *, startup=False):
        report = self._commodity_coordinator.run_startup(self._now()) if startup else self._commodity_coordinator.run_scheduled(self._now())
        status = report.get('status','skipped') if isinstance(report,dict) else 'skipped'
        if status not in {'skipped','not_due','disabled','not_selected'}:
            self._set_degraded('commodities',status not in {'completed','no_change'})
        self._logger.emit('scheduler.commodities.'+('completed' if status in {'completed','no_change'} else 'skipped' if status in {'not_due','skipped'} else 'failed'),
            'info' if status in {'completed','no_change','not_due','skipped'} else 'warning',status=status)
        return report

    def _run_etf(self, *, startup=False):
        report = self._etf_coordinator.run_startup(self._now()) if startup else self._etf_coordinator.run_scheduled(self._now())
        status = report.get('status','skipped') if isinstance(report,dict) else 'skipped'
        if status not in {'skipped','not_due','disabled','not_selected'}:
            self._set_degraded('etf',status not in {'completed','no_change'})
        self._logger.emit('scheduler.etf.'+('completed' if status in {'completed','no_change'} else 'skipped' if status in {'not_due','skipped'} else 'failed'),
            'info' if status in {'completed','no_change','not_due','skipped'} else 'warning',status=status)
        return report

    def _run_region(self, *, startup=False):
        report = self._region_coordinator.run_startup(self._now()) if startup else self._region_coordinator.run_scheduled(self._now())
        status = report.get('status','skipped') if isinstance(report,dict) else 'skipped'
        if status not in {'skipped','not_due','disabled','not_selected'}:
            self._set_degraded('region',status not in {'completed','no_change'})
        self._logger.emit('scheduler.region.'+('completed' if status in {'completed','no_change'} else 'skipped' if status in {'not_due','skipped'} else 'failed'),
            'info' if status in {'completed','no_change','not_due','skipped'} else 'warning',status=status)
        return report

    def _run_pulse(self, *, startup=False):
        report = self._pulse_coordinator.run_startup(self._now()) if startup else self._pulse_coordinator.run_scheduled(self._now())
        status = report.get('status', 'skipped') if isinstance(report, dict) else 'skipped'
        if status not in {'skipped','not_due','disabled','not_selected'}:
            self._set_degraded('pulse', status not in {'completed','no_change'})
        self._logger.emit('scheduler.pulse.' + ('completed' if status in {'completed','no_change'} else 'skipped' if status in {'not_due','skipped'} else 'failed'),
            'info' if status in {'completed','no_change','not_due','skipped'} else 'warning', status=status)
        return report

    def _run_market(self, *, startup=False):
        report = self._market_coordinator.run_startup(self._now()) if startup else self._market_coordinator.run_scheduled(self._now())
        status = report.get('status', 'skipped') if isinstance(report, dict) else 'skipped'
        if status not in {'skipped','not_due','disabled','not_selected'}:
            self._set_degraded('market', status not in {'completed','no_change'})
        self._logger.emit('scheduler.market.' + ('completed' if status in {'completed','no_change'} else 'skipped' if status in {'not_due','skipped'} else 'failed'),
            'info' if status in {'completed','no_change','not_due','skipped'} else 'warning', status=status)
        return report

    def _run_macro_startup(self) -> None:
        if self._macro_coordinator is not None:
            self._guard_job(lambda: self._run_macro(startup=True), "macro")()

    def _run_macro(self, *, startup: bool = False) -> object:
        assert self._macro_coordinator is not None
        report = (
            self._macro_coordinator.run_startup(self._now()) if startup
            else self._macro_coordinator.run_scheduled(self._now())
        )
        status = getattr(report, "status", "skipped")
        if status in {"partial", "failed", "completed"}:
            self._set_degraded("macro", status != "completed")
        assert self._logger is not None
        self._logger.emit(
            "scheduler.macro.completed" if status == "completed" else
            "scheduler.macro.failed" if status in {"partial", "failed"} else
            "scheduler.macro.skipped",
            "error" if status == "failed" else "warning" if status == "partial" else "info",
            status=status,
        )
        return report

    def _run_startup(self) -> object:
        assert self._logger is not None
        assert self._coordinator is not None
        now = self._now()
        self._logger.emit("scheduler.catchup.started", "info")
        try:
            report = self._coordinator.run_startup(now)
        except Exception as error:
            self._degrade_after_error("catchup")
            self._log_job_failure("scheduler.catchup.failed", error)
            raise
        attempted = tuple(getattr(report, "attempted_dates", ()))
        remaining = tuple(getattr(report, "remaining_dates", ()))
        try:
            calendar_unavailable = self._calendar_alert_is_open()
        except Exception as error:
            self._degrade_after_error("catchup")
            self._log_job_failure("scheduler.catchup.failed", error)
            return report
        if calendar_unavailable:
            self._set_degraded("calendar", True)
            self._logger.emit(
                "scheduler.catchup.failed",
                "error",
                error_code="TRADE_CALENDAR_UNAVAILABLE",
                message="trade calendar unavailable",
            )
            return report
        self._set_degraded("calendar", False)
        if not attempted and not remaining:
            self._logger.emit("scheduler.catchup.skipped", "info", status="skipped")
        if remaining:
            self._set_degraded("catchup", True)
            if len(attempted) >= self._settings.sync_catchup_max_days:
                self._logger.emit(
                    "scheduler.catchup.limit-exceeded",
                    "warning",
                    attempted_count=len(attempted),
                    remaining_count=len(remaining),
                )
        else:
            self._set_degraded("catchup", False)
        self._logger.emit(
            "scheduler.catchup.completed",
            "warning" if remaining else "info",
            status="degraded" if remaining else "completed",
            attempted_count=len(attempted),
            remaining_count=len(remaining),
        )
        return report

    def _run_heartbeat(self) -> None:
        assert self._instance_id is not None
        assert self._logger is not None
        try:
            with self._state_transition_lock:
                heartbeat = self._state.heartbeat(self._instance_id)
                persisted_state = getattr(heartbeat, "state", "running")
                state = (
                    self._operational_state
                    if self._degraded_reasons or self._state_write_pending
                    else persisted_state
                )
                if state not in {"running", "degraded"}:
                    state = "running"
                daily = self._scheduler.get_job("sync-daily") if self._scheduler else None
                next_run_at = getattr(daily, "next_run_time", None)
                self._state.touch_instance(
                    self._instance_id,
                    now=self._now(),
                    next_run_at=next_run_at,
                    state=state,
                )
                if self._degraded_reasons or self._state_write_pending:
                    self._state_write_pending = False
            self._logger.emit(
                "scheduler.heartbeat",
                "debug",
                status=state,
                next_run_at=next_run_at.isoformat() if next_run_at else None,
            )
        except Exception as error:
            self._log_job_failure("scheduler.heartbeat", error)

    def _run_freshness(self) -> None:
        assert self._logger is not None
        try:
            report = self._freshness_runner(self._now())
            missing_dates = getattr(report, "missing_business_dates", None)
            if missing_dates is not None:
                self._set_degraded("catchup", bool(missing_dates))
            self._set_degraded(
                "freshness",
                getattr(report, "healthy", True) is False,
            )
            self._logger.emit(
                "scheduler.freshness.completed",
                "info",
                status="completed",
                stale_products=getattr(report, "stale_products", None),
                missing_products=getattr(report, "missing_products", None),
            )
        except Exception as error:
            self._degrade_after_error("freshness")
            self._log_job_failure("scheduler.freshness.failed", error)

    def _log_job_failure(
        self, event: str, error: Exception, **fields: object,
    ) -> None:
        assert self._logger is not None
        try:
            self._logger.emit(
                event, "error", error_code=_error_code(error),
                message="scheduler job failed", **fields,
            )
        except BaseException:
            # Preserve the primary failure for the executor boundary, even when
            # logging fails while handling an otherwise recoverable interval.
            raise error

    def _degrade_after_error(self, reason: str) -> None:
        # Only called with an existing business exception. A secondary failure
        # (including an interrupted write) must not replace that primary error.
        try:
            self._set_degraded(reason, True)
        except BaseException:
            self._state_write_pending = True

    def _set_degraded(self, reason: str, active: bool) -> None:
        with self._state_transition_lock:
            if self._instance_id is None:
                return
            was_degraded = bool(self._degraded_reasons)
            if active:
                self._degraded_reasons.add(reason)
            else:
                self._degraded_reasons.discard(reason)
            is_degraded = bool(self._degraded_reasons)
            if was_degraded == is_degraded and not self._state_write_pending:
                return
            try:
                heartbeat = self._state.heartbeat(self._instance_id)
                next_run_at = getattr(heartbeat, "next_run_at", None)
                self._state.touch_instance(
                    self._instance_id,
                    now=self._now(),
                    next_run_at=next_run_at,
                    state=self._operational_state,
                )
            except Exception:
                self._state_write_pending = True
            else:
                self._state_write_pending = False

    @property
    def _operational_state(self) -> str:
        return "degraded" if self._degraded_reasons else "running"

    def _calendar_alert_is_open(self) -> bool:
        alert = self._state.alert(self._calendar_alert_key)
        return bool(
            alert is not None
            and getattr(alert, "status", None) == "open"
            and getattr(alert, "alert_type", None)
            == "trade-calendar-unavailable"
        )

    @property
    def _calendar_alert_key(self) -> str:
        return (
            "scheduler:trade-calendar-unavailable:"
            f"{self._universe.profile}:{self._universe.version}"
        )

    def _stop(
        self,
        scheduler: BlockingScheduler | None,
        *,
        primary_error: BaseException | None,
        suppress_cleanup_error: bool,
    ) -> None:
        cleanup_error: BaseException | None = None
        with self._jobs_condition:
            self._jobs_stopping = True
        if scheduler is not None and scheduler.running:
            try:
                scheduler.pause()
            except BaseException as error:
                cleanup_error = error
        # APScheduler shutdown holds its job store lock while joining workers.
        # Drain first, while heartbeat get_job() can still acquire that lock.
        with self._jobs_condition:
            while self._active_jobs:
                try:
                    self._jobs_condition.wait()
                except (KeyboardInterrupt, SystemExit):
                    continue
        if scheduler is not None and scheduler.running:
            try:
                scheduler.shutdown(wait=True)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if self._logger is not None:
            try:
                self._logger.emit("scheduler.stopping", "info")
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if self._instance_id is not None:
            try:
                self._state.stop_instance(self._instance_id, now=self._now())
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if (
            cleanup_error is not None
            and primary_error is None
            and not suppress_cleanup_error
        ):
            raise cleanup_error

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock must return a timezone-aware datetime")
        return value

def _error_code(error: BaseException) -> str:
    name = type(error).__name__
    return "".join(
        ("_" + character if character.isupper() and index else character).upper()
        for index, character in enumerate(name)
    )


def _noop_job() -> None:
    return None


def build_scheduler_runtime(
    settings: Settings,
    daily_runtime: DailySyncRuntime,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SchedulerRuntime:
    state = SchedulerStateRepository(daily_runtime.session_factory)
    resolver = ExpectedBusinessDateResolver(
        state,
        settings.sync_timezone,
        settings.sync_hour,
        settings.sync_minute,
    )
    planner = CatchupPlanner(
        state,
        daily_runtime.selection,
        max_days=settings.sync_catchup_max_days,
    )
    freshness = FreshnessService(
        daily_runtime.session_factory,
        timezone=settings.sync_timezone,
    )

    def coordinator_factory(instance_id: str) -> ScheduledDailySyncCoordinator:
        return ScheduledDailySyncCoordinator(
            state=state,
            planner=planner,
            resolver=resolver,
            job=daily_runtime.job,
            freshness=freshness,
            universe=daily_runtime.selection,
            instance_id=instance_id,
            mode=daily_runtime.mode,
            timezone=settings.sync_timezone,
            heartbeat_timeout=timedelta(
                seconds=settings.scheduler_heartbeat_timeout_seconds
            ),
            failure_alert_threshold=settings.sync_failure_alert_threshold,
            lookback_days=settings.sync_disclosure_lookback_days,
        )

    def prepare_checkpoint(now: datetime) -> object:
        try:
            expected = resolver.resolve(now)
            return planner.prepare_startup(expected, now)
        except TradeCalendarUnavailable:
            return False

    def refresh(now: datetime) -> object:
        expected = resolver.resolve(now)
        plan = planner.inspect(expected)
        report = freshness.refresh(
            expected_business_date=expected,
            missing_business_dates=plan.missing_dates,
            checked_at=now,
        )
        if not plan.missing_dates:
            state.resolve_alert(
                "scheduler:catchup-backlog:"
                f"{daily_runtime.selection.profile}:{daily_runtime.selection.version}",
                now,
            )
        return report

    token = (
        settings.tushare_token.get_secret_value()
        if settings.tushare_token is not None
        else ""
    )
    database_url = settings.database_url or ""
    database_read_url = settings.database_read_url or ""

    macro_coordinator = None
    if settings.macro_sync_enabled:
        with daily_runtime.session_factory() as session:
            engine = session.get_bind()
        macro_coordinator = MacroScheduledCoordinator(
            settings=settings, catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE),
            history=SqlMacroSyncHistory(daily_runtime.session_factory), state=state,
            runtime_factory=lambda: open_macro_sync_runtime(settings),
            publication_lock=job_lock_for_engine(engine), clock=clock,
        )

    def logger_factory(instance_id: str) -> SchedulerJsonLogger:
        return SchedulerJsonLogger(
            instance_id,
            sanitize=lambda message: redact_sensitive_values(
                message,
                token,
                database_url,
                database_read_url,
            ),
            clock=clock,
        )

    return SchedulerRuntime(
        settings=settings,
        state=state,
        universe=daily_runtime.selection,
        coordinator_factory=coordinator_factory,
        prepare_checkpoint=prepare_checkpoint,
        freshness_runner=refresh,
        clock=clock,
        logger_factory=logger_factory,
        macro_coordinator=macro_coordinator,
        market_coordinator=MarketScheduledCoordinator(settings) if settings.routine_market_enabled else None,
        pulse_coordinator=PulseScheduledCoordinator(settings) if settings.routine_market_pulse_enabled else None,
        region_coordinator=RegionScheduledCoordinator(settings) if settings.routine_region_enabled else None,
        etf_coordinator=EtfScheduledCoordinator(settings) if settings.routine_etf_heatmap_enabled else None,
        commodity_coordinator=CommodityScheduledCoordinator(settings) if settings.routine_commodities_enabled else None,
    )


def _validate_startup_settings(settings: Settings, *, confirm_full_market: bool = False) -> None:
    if settings.data_backend != "database":
        raise RuntimeError("scheduler requires the database backend")
    if not settings.database_url:
        raise RuntimeError("scheduler requires DANG_DATABASE_URL")
    if settings.tushare_token is None:
        raise RuntimeError("scheduler requires DANG_TUSHARE_TOKEN")
    resolve_cli_selection(settings, confirm_full_market=confirm_full_market)
    if settings.sync_universe == "custom":
        if settings.sync_universe_file is None:
            raise RuntimeError("custom universe requires DANG_SYNC_UNIVERSE_FILE")
        if not settings.sync_universe_file.is_file():
            raise RuntimeError("custom universe file does not exist")
    elif settings.sync_universe_file is not None:
        raise RuntimeError("DANG_SYNC_UNIVERSE_FILE is only valid for custom universe")


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(prog="app.jobs.scheduler", description="显式启动配置驱动的常驻调度；关机期间不运行。")
    parser.add_argument("--confirm-full-market", action="store_true", help="本次明确授权all股票池自动同步")
    arguments = parser.parse_args([] if argv is None else argv)
    token = configured_tushare_token()
    database_url = ""
    database_read_url = ""
    try:
        settings = Settings.from_env()
    except Exception:
        _emit_startup_failure(
            "SCHEDULER_SETTINGS_INVALID",
            "scheduler settings could not be loaded",
            sanitize=lambda message: redact_sensitive_values(message, token),
        )
        print("scheduler settings could not be loaded", file=sys.stderr)
        return 1

    database_url = settings.database_url or ""
    database_read_url = settings.database_read_url or ""
    if settings.tushare_token is not None:
        token = settings.tushare_token.get_secret_value()

    def sanitize(message: str) -> str:
        return redact_sensitive_values(
            message,
            token,
            database_url,
            database_read_url,
        )

    runtime_completed = False
    manager = None
    try:
        _validate_startup_settings(settings, confirm_full_market=arguments.confirm_full_market)
        candidate_manager = open_daily_sync_runtime(
            settings,
            settings.sync_universe,
            settings.sync_universe_file,
        )
        daily_runtime = candidate_manager.__enter__()
        manager = candidate_manager
        scheduler_runtime = build_scheduler_runtime(settings, daily_runtime)
        try:
            scheduler_runtime.run()
        except BaseException as primary_error:
            try:
                manager.__exit__(
                    type(primary_error),
                    primary_error,
                    primary_error.__traceback__,
                )
            except BaseException:
                pass
            manager = None
            raise
        runtime_completed = True
        active_manager = manager
        manager = None
        active_manager.__exit__(None, None, None)
        return 0
    except Exception as error:
        if not runtime_completed:
            _emit_startup_failure(
                "SCHEDULER_STARTUP_FAILED",
                "scheduler startup failed",
                sanitize=sanitize,
            )
        diagnostic = redact_sensitive_values(
            f"{type(error).__name__}: {error}",
            token,
            database_url,
            database_read_url,
        )
        print(diagnostic, file=sys.stderr)
        return 1
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except BaseException:
                pass


def _emit_startup_failure(
    error_code: str,
    message: str,
    *,
    sanitize: Callable[[str], str],
) -> None:
    try:
        SchedulerJsonLogger(None, sanitize=sanitize).emit(
            "scheduler.startup.failed",
            "error",
            error_code=error_code,
            message=message,
        )
    except BaseException:
        pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
