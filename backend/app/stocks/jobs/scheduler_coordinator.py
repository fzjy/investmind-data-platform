from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.connectors.tushare.strategies import TushareMode
from app.stocks.jobs.daily_sync import (
    DailySyncExecutionContext,
    DailySyncJob,
    DailySyncOptions,
    DailySyncReport,
)
from app.jobs.freshness import FreshnessService
from app.stocks.jobs.scheduler_planning import (
    CatchupPlanner,
    ExpectedBusinessDateResolver,
    TradeCalendarUnavailable,
)
from app.jobs.scheduler_state import SchedulerStateRepository
from app.stocks.universes.models import UniverseSelection


@dataclass(frozen=True, slots=True)
class CatchupExecutionReport:
    attempted_dates: tuple[date, ...]
    remaining_dates: tuple[date, ...]
    reports: tuple[DailySyncReport, ...]


class ScheduledDailySyncCoordinator:
    def __init__(
        self,
        *,
        state: SchedulerStateRepository,
        planner: CatchupPlanner,
        resolver: ExpectedBusinessDateResolver,
        job: DailySyncJob,
        freshness: FreshnessService,
        universe: UniverseSelection,
        instance_id: str,
        mode: TushareMode,
        timezone: str,
        heartbeat_timeout: timedelta,
        failure_alert_threshold: int,
        lookback_days: int,
    ) -> None:
        self._state = state
        self._planner = planner
        self._resolver = resolver
        self._job = job
        self._freshness = freshness
        self._universe = universe
        self._instance_id = instance_id
        self._mode = mode
        self._timezone = ZoneInfo(timezone)
        self._timezone_name = timezone
        self._heartbeat_timeout = heartbeat_timeout
        self._failure_alert_threshold = failure_alert_threshold
        self._lookback_days = lookback_days

    def recover_interrupted(self, now: datetime) -> tuple[str, ...]:
        self._require_timezone_aware(now)
        recovered = self._state.recover_interrupted_runs(
            self._universe,
            now=now,
            timeout=self._heartbeat_timeout,
        )
        if recovered:
            self.evaluate_failures(now)
        return recovered

    def run_startup(self, now: datetime) -> CatchupExecutionReport:
        self._require_timezone_aware(now)
        expected = self._resolve_expected(now, resolve_calendar_alert=False)
        if expected is None:
            return CatchupExecutionReport((), (), ())
        initial = self._planner.plan_startup(expected, now)
        attempted_dates = tuple(sorted(initial.selected_dates))
        reports: list[DailySyncReport] = []
        operational_errors: list[Exception] = []
        for business_date in attempted_dates:
            try:
                reports.append(
                    self._run_one(
                        business_date,
                        now,
                        expected_business_date=expected,
                    )
                )
            except Exception as error:
                operational_errors.append(error)

        try:
            remaining_dates = self._planner.inspect(expected).missing_dates
        except Exception as error:
            operational_errors.append(error)
            remaining_dates = initial.missing_dates
        else:
            try:
                self._update_backlog_alert(
                    now,
                    initial_count=len(initial.missing_dates),
                    selected_count=len(attempted_dates),
                    remaining_dates=remaining_dates,
                    expected_business_date=expected,
                    max_days=initial.max_days,
                )
            except Exception as error:
                operational_errors.append(error)

        try:
            self._state.resolve_alert(self._calendar_alert_key, now)
        except Exception as error:
            operational_errors.append(error)

        if operational_errors:
            raise operational_errors[0]
        return CatchupExecutionReport(
            attempted_dates=attempted_dates,
            remaining_dates=remaining_dates,
            reports=tuple(reports),
        )

    def run_scheduled(self, now: datetime) -> DailySyncReport | None:
        self._require_timezone_aware(now)
        business_date = now.astimezone(self._timezone).date()
        trading_day = self._state.trading_day(business_date)
        if trading_day is None:
            self._open_calendar_alert(now, business_date)
            return None
        if trading_day is False:
            self._state.resolve_alert(self._calendar_alert_key, now)
            return None
        try:
            report = self.run_one(business_date, now)
        except Exception:
            try:
                self._state.resolve_alert(self._calendar_alert_key, now)
            except Exception:
                pass
            raise
        self._state.resolve_alert(self._calendar_alert_key, now)
        return report

    def run_one(self, business_date: date, now: datetime) -> DailySyncReport:
        return self._run_one(business_date, now, expected_business_date=None)

    def evaluate_failures(self, now: datetime) -> int:
        count = self._state.consecutive_failures(self._universe)
        latest = self._state.latest_daily_run(
            self._universe,
            statuses=("failed", "completed", "partial"),
        )
        latest_error_code = (
            latest.details.get("errorCode") if latest is not None else None
        )
        error_code = (
            latest_error_code
            if isinstance(latest_error_code, str)
            else "daily_sync_failed"
        )
        if count >= self._failure_alert_threshold:
            self._state.open_alert(
                self._failure_alert_key,
                "consecutive-failures",
                "critical",
                now,
                {
                    "threshold": self._failure_alert_threshold,
                    "consecutiveFailures": count,
                    "latestBusinessDate": (
                        latest.business_date.isoformat() if latest is not None else None
                    ),
                    "errorCode": error_code,
                    "universeProfile": self._universe.profile,
                    "universeVersion": self._universe.version,
                },
            )
        elif latest is not None and latest.status in {"completed", "partial"}:
            self._state.resolve_alert(self._failure_alert_key, now)
        return count

    def _run_one(
        self,
        business_date: date,
        now: datetime,
        *,
        expected_business_date: date | None,
    ) -> DailySyncReport:
        report = self._job.run(
            DailySyncOptions(
                business_date=business_date,
                mode=self._mode,
                lookback_days=self._lookback_days,
                execution=DailySyncExecutionContext(
                    trigger="scheduler",
                    scheduler_instance_id=self._instance_id,
                ),
            )
        )
        try:
            if report.status in {"completed", "partial"}:
                expected = expected_business_date or self._resolve_expected(now)
                if expected is not None:
                    missing_dates = self._planner.inspect(expected).missing_dates
                    self._freshness.refresh(
                        expected_business_date=expected,
                        missing_business_dates=missing_dates,
                        checked_at=now,
                    )
        except BaseException:
            try:
                self.evaluate_failures(now)
            except BaseException:
                pass
            raise
        else:
            self.evaluate_failures(now)
        return report

    def _resolve_expected(
        self,
        now: datetime,
        *,
        resolve_calendar_alert: bool = True,
    ) -> date | None:
        try:
            expected = self._resolver.resolve(now)
        except TradeCalendarUnavailable:
            self._require_timezone_aware(now)
            self._open_calendar_alert(now, now.astimezone(self._timezone).date())
            return None
        if resolve_calendar_alert:
            self._state.resolve_alert(self._calendar_alert_key, now)
        return expected

    def _open_calendar_alert(self, now: datetime, business_date: date) -> None:
        self._state.open_alert(
            self._calendar_alert_key,
            "trade-calendar-unavailable",
            "warning",
            now,
            {
                "businessDate": business_date.isoformat(),
                "errorCode": TradeCalendarUnavailable.error_code,
                "timezone": self._timezone_name,
                "universeProfile": self._universe.profile,
                "universeVersion": self._universe.version,
            },
        )

    def _update_backlog_alert(
        self,
        now: datetime,
        *,
        initial_count: int,
        selected_count: int,
        remaining_dates: tuple[date, ...],
        expected_business_date: date,
        max_days: int,
    ) -> None:
        if not remaining_dates:
            self._state.resolve_alert(self._catchup_alert_key, now)
            return
        self._state.open_alert(
            self._catchup_alert_key,
            "catchup-backlog",
            "warning",
            now,
            {
                "totalCount": initial_count,
                "selectedCount": selected_count,
                "remainingCount": len(remaining_dates),
                "earliestMissingDate": remaining_dates[0].isoformat(),
                "latestMissingDate": remaining_dates[-1].isoformat(),
                "expectedBusinessDate": expected_business_date.isoformat(),
                "maxDays": max_days,
                "limitExceeded": initial_count > max_days,
                "universeProfile": self._universe.profile,
                "universeVersion": self._universe.version,
            },
        )

    @property
    def _failure_alert_key(self) -> str:
        return (
            "sync-daily:consecutive-failures:"
            f"{self._universe.profile}:{self._universe.version}"
        )

    @property
    def _calendar_alert_key(self) -> str:
        return (
            "scheduler:trade-calendar-unavailable:"
            f"{self._universe.profile}:{self._universe.version}"
        )

    @property
    def _catchup_alert_key(self) -> str:
        return (
            "scheduler:catchup-backlog:"
            f"{self._universe.profile}:{self._universe.version}"
        )

    @staticmethod
    def _require_timezone_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
