from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.stocks.jobs.scheduler_planning import (
    CatchupPlanner,
    ExpectedBusinessDateResolver,
    TradeCalendarUnavailable,
)
from app.jobs.scheduler_state import CheckpointView
from app.stocks.universes.models import UniverseSelection


NOW = datetime(2026, 9, 5, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class FakeCalendarRepository:
    def __init__(self, open_dates: set[date]) -> None:
        self._open_dates = open_dates

    def latest_open_date(self, *, on_or_before: date) -> date | None:
        eligible = [business_date for business_date in self._open_dates if business_date <= on_or_before]
        return max(eligible, default=None)

    def calendar_covers(self, *, start: date, end: date) -> bool:
        # 此测试夹具声明区间内其余日期均有闭市记录，不以业务代码推算交易日。
        if start > end:
            return True
        return bool(self._open_dates) and min(self._open_dates) <= start and end <= max(max(self._open_dates), date(2026, 9, 6))


class FakeStateRepository(FakeCalendarRepository):
    def __init__(self, *, open_dates: tuple[date, ...], completed: set[date]) -> None:
        super().__init__(set(open_dates))
        self._completed = frozenset(completed)
        self._checkpoint: CheckpointView | None = None
        self.ensure_checkpoint_calls: list[tuple[date, datetime]] = []
        self.update_checkpoint_calls: list[tuple[str, datetime, dict[str, object]]] = []
        self.open_dates_calls: list[tuple[date, date]] = []

    def checkpoint(self, universe: UniverseSelection) -> CheckpointView | None:
        return self._checkpoint

    def latest_daily_run(self, universe, *, statuses=None):
        from types import SimpleNamespace
        assert statuses == ("completed",)
        return SimpleNamespace(business_date=max(self._completed)) if self._completed else None

    def ensure_checkpoint(
        self,
        universe: UniverseSelection,
        *,
        fallback: date,
        now: datetime,
    ) -> CheckpointView:
        self.ensure_checkpoint_calls.append((fallback, now))
        if self._checkpoint is None:
            managed_from = max(self._completed, default=fallback)
            self._checkpoint = CheckpointView(
                key="fake-checkpoint",
                managed_from=managed_from,
                created_at=now,
                last_evaluated_at=now,
                details={},
            )
        return self._checkpoint

    def update_checkpoint_evaluation(
        self,
        key: str,
        *,
        now: datetime,
        details: dict[str, object],
    ) -> None:
        self.update_checkpoint_calls.append((key, now, details))

    def open_dates(self, *, start: date, end: date) -> tuple[date, ...]:
        self.open_dates_calls.append((start, end))
        return tuple(
            reversed(
                sorted(
                    business_date
                    for business_date in self._open_dates
                    if start <= business_date <= end
                )
            )
        )

    def completed_dates(
        self,
        universe: UniverseSelection,
        dates: tuple[date, ...],
    ) -> frozenset[date]:
        return self._completed.intersection(dates)

    def set_checkpoint(self, managed_from: date) -> None:
        self._checkpoint = CheckpointView(
            key="fake-checkpoint",
            managed_from=managed_from,
            created_at=NOW,
            last_evaluated_at=NOW,
            details={},
        )


@pytest.fixture
def selection() -> UniverseSelection:
    return UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )


@pytest.mark.parametrize(
    ("local_time", "query_date", "expected"),
    [
        (time(18, 59), date(2026, 9, 4), date(2026, 9, 3)),
        (time(19, 0), date(2026, 9, 4), date(2026, 9, 4)),
        (time(23, 0), date(2026, 9, 6), date(2026, 9, 4)),
    ],
)
def test_expected_business_date_uses_configured_cutoff(
    local_time: time,
    query_date: date,
    expected: date,
) -> None:
    repository = FakeCalendarRepository({date(2026, 9, 3), date(2026, 9, 4)})
    now = datetime.combine(query_date, local_time, ZoneInfo("Asia/Shanghai"))

    assert ExpectedBusinessDateResolver(repository, "Asia/Shanghai", 19, 0).resolve(now) == expected


def test_expected_business_date_converts_an_aware_timestamp_to_configured_timezone() -> None:
    repository = FakeCalendarRepository({date(2026, 9, 3), date(2026, 9, 4)})
    now = datetime(2026, 9, 4, 11, 0, tzinfo=ZoneInfo("UTC"))

    assert ExpectedBusinessDateResolver(repository, "Asia/Shanghai", 19, 0).resolve(now) == date(2026, 9, 4)


def test_expected_business_date_never_guesses_when_calendar_is_missing() -> None:
    with pytest.raises(TradeCalendarUnavailable) as error:
        ExpectedBusinessDateResolver(FakeCalendarRepository(set()), "Asia/Shanghai", 19, 0).resolve(NOW)

    assert error.value.error_code == "TRADE_CALENDAR_UNAVAILABLE"


def test_catchup_plan_selects_oldest_ten_and_reports_remaining(selection: UniverseSelection) -> None:
    open_dates = tuple(date(2026, 8, 20) + timedelta(days=index) for index in range(12))
    repository = FakeStateRepository(open_dates=open_dates, completed={open_dates[0]})

    plan = CatchupPlanner(repository, selection, max_days=10).plan_startup(
        expected=open_dates[-1], now=NOW
    )

    assert plan.managed_from == open_dates[0]
    assert plan.missing_dates == open_dates[1:]
    assert plan.selected_dates == open_dates[1:11]
    assert plan.backlog_count == 11
    assert plan.limit_exceeded is True


def test_first_checkpoint_does_not_create_pre_activation_history(selection: UniverseSelection) -> None:
    expected = date(2026, 9, 5)
    repository = FakeStateRepository(open_dates=(expected,), completed=set())

    plan = CatchupPlanner(repository, selection, max_days=10).plan_startup(expected=expected, now=NOW)

    assert plan.managed_from == expected
    assert plan.selected_dates == (expected,)


def test_completed_latest_date_does_not_hide_an_intermediate_gap(selection: UniverseSelection) -> None:
    open_dates = (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4))
    repository = FakeStateRepository(
        open_dates=open_dates,
        completed={date(2026, 9, 1), date(2026, 9, 4)},
    )
    repository.set_checkpoint(date(2026, 9, 1))

    plan = CatchupPlanner(repository, selection, max_days=10).plan_startup(
        expected=date(2026, 9, 4), now=NOW
    )

    assert plan.missing_dates == (date(2026, 9, 2), date(2026, 9, 3))
    assert plan.selected_dates == plan.missing_dates


def test_startup_writes_json_serializable_evaluation_details(selection: UniverseSelection) -> None:
    expected = date(2026, 9, 5)
    repository = FakeStateRepository(open_dates=(expected,), completed=set())

    CatchupPlanner(repository, selection, max_days=10).plan_startup(expected=expected, now=NOW)

    assert len(repository.update_checkpoint_calls) == 1
    _, update_time, details = repository.update_checkpoint_calls[0]
    assert update_time == NOW
    assert details["expected_business_date"] == "2026-09-05"
    assert details["managed_from"] == "2026-09-05"
    assert details["selected_dates"] == ["2026-09-05"]
    json.dumps(details)


def test_inspect_without_checkpoint_does_not_write(selection: UniverseSelection) -> None:
    expected = date(2026, 9, 5)
    repository = FakeStateRepository(open_dates=(expected,), completed=set())

    plan = CatchupPlanner(repository, selection, max_days=10).inspect(expected)

    assert plan.managed_from == expected
    assert repository.ensure_checkpoint_calls == []
    assert repository.update_checkpoint_calls == []


def test_inspect_with_checkpoint_uses_persisted_start_and_does_not_write(
    selection: UniverseSelection,
) -> None:
    open_dates = (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3))
    repository = FakeStateRepository(open_dates=open_dates, completed={date(2026, 9, 1)})
    repository.set_checkpoint(date(2026, 9, 1))

    plan = CatchupPlanner(repository, selection, max_days=10).inspect(date(2026, 9, 3))

    assert plan.managed_from == date(2026, 9, 1)
    assert plan.expected_business_date == date(2026, 9, 3)
    assert plan.missing_dates == (date(2026, 9, 2), date(2026, 9, 3))
    assert plan.selected_dates == (date(2026, 9, 2), date(2026, 9, 3))
    assert repository.ensure_checkpoint_calls == []
    assert repository.update_checkpoint_calls == []


def test_inspect_skips_calendar_query_when_checkpoint_starts_after_expected(
    selection: UniverseSelection,
) -> None:
    expected = date(2026, 9, 5)
    managed_from = date(2026, 9, 6)
    repository = FakeStateRepository(open_dates=(expected, managed_from), completed=set())
    repository.set_checkpoint(managed_from)

    plan = CatchupPlanner(repository, selection, max_days=10).inspect(expected)

    assert plan.expected_business_date == expected
    assert plan.managed_from == managed_from
    assert plan.missing_dates == ()
    assert plan.selected_dates == ()
    assert repository.open_dates_calls == []
