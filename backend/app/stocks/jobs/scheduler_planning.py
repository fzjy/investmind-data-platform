from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.jobs.scheduler_state import CheckpointView, SchedulerStateRepository
from app.stocks.universes.models import UniverseSelection


class TradeCalendarUnavailable(RuntimeError):
    error_code = "TRADE_CALENDAR_UNAVAILABLE"


class TradeCalendarIncomplete(TradeCalendarUnavailable):
    error_code = "TRADE_CALENDAR_INCOMPLETE"


class ExpectedBusinessDateResolver:
    def __init__(
        self,
        repository: SchedulerStateRepository,
        timezone: str,
        hour: int,
        minute: int,
    ) -> None:
        self._repository = repository
        self._timezone = ZoneInfo(timezone)
        self._hour = hour
        self._minute = minute

    def resolve(self, now: datetime) -> date:
        _require_timezone_aware(now)
        local_now = now.astimezone(self._timezone)
        cutoff = time(self._hour, self._minute)
        upper = (
            local_now.date()
            if local_now.time() >= cutoff
            else local_now.date() - timedelta(days=1)
        )
        resolved = self._repository.latest_open_date(on_or_before=upper)
        if resolved is None:
            raise TradeCalendarUnavailable("trade calendar has no open date before cutoff")
        if not self._repository.calendar_covers(start=resolved, end=upper):
            raise TradeCalendarIncomplete("trade calendar does not cover cutoff")
        return resolved


@dataclass(frozen=True, slots=True)
class CatchupPlan:
    expected_business_date: date
    managed_from: date
    missing_dates: tuple[date, ...]
    selected_dates: tuple[date, ...]
    max_days: int

    @property
    def backlog_count(self) -> int:
        return len(self.missing_dates)

    @property
    def limit_exceeded(self) -> bool:
        return self.backlog_count > self.max_days


class CatchupPlanner:
    def __init__(
        self,
        repository: SchedulerStateRepository,
        universe: UniverseSelection,
        *,
        max_days: int,
    ) -> None:
        self._repository = repository
        self._universe = universe
        self._max_days = max_days

    def plan_startup(self, expected: date, now: datetime) -> CatchupPlan:
        checkpoint = self.prepare_startup(expected, now)
        plan = self._plan(expected, checkpoint.managed_from)
        self._repository.update_checkpoint_evaluation(
            checkpoint.key,
            now=now,
            details=_evaluation_details(plan),
        )
        return plan

    def prepare_startup(self, expected: date, now: datetime) -> CheckpointView:
        """启动钩子和补跑规划共用：先验证受管日历，再创建检查点。"""
        _require_timezone_aware(now)
        existing = self._repository.checkpoint(self._universe)
        if existing is not None:
            managed_from = existing.managed_from
        else:
            completed = self._repository.latest_daily_run(self._universe, statuses=("completed",))
            managed_from = completed.business_date if completed is not None else expected
        self._require_calendar(managed_from, expected)
        return self._repository.ensure_checkpoint(
            self._universe,
            fallback=expected,
            now=now,
        )

    def inspect(self, expected: date) -> CatchupPlan:
        checkpoint = self._repository.checkpoint(self._universe)
        managed_from = checkpoint.managed_from if checkpoint is not None else expected
        return self._plan(expected, managed_from)

    def _plan(self, expected: date, managed_from: date) -> CatchupPlan:
        self._require_calendar(managed_from, expected)
        open_dates = (
            tuple(
                sorted(
                    self._repository.open_dates(start=managed_from, end=expected)
                )
            )
            if managed_from <= expected
            else ()
        )
        completed = self._repository.completed_dates(self._universe, open_dates)
        missing_dates = tuple(
            business_date
            for business_date in open_dates
            if business_date not in completed
        )
        return CatchupPlan(
            expected_business_date=expected,
            managed_from=managed_from,
            missing_dates=missing_dates,
            selected_dates=missing_dates[: self._max_days],
            max_days=self._max_days,
        )

    def _require_calendar(self, start: date, end: date) -> None:
        if not self._repository.calendar_covers(start=start, end=end):
            raise TradeCalendarIncomplete("trade calendar does not cover managed window")


def _require_timezone_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def _evaluation_details(plan: CatchupPlan) -> dict[str, object]:
    return {
        "expected_business_date": plan.expected_business_date.isoformat(),
        "managed_from": plan.managed_from.isoformat(),
        "missing_dates": [business_date.isoformat() for business_date in plan.missing_dates],
        "selected_dates": [business_date.isoformat() for business_date in plan.selected_dates],
        "backlog_count": plan.backlog_count,
        "max_days": plan.max_days,
        "limit_exceeded": plan.limit_exceeded,
    }
