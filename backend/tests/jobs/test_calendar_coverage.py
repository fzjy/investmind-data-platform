from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, func

from app.db.base import Base
from app.db.models import SchedulerCheckpoint, TradeCalendar
from app.db.session import create_engine_and_session
from app.stocks.jobs.scheduler_planning import CatchupPlanner, ExpectedBusinessDateResolver, TradeCalendarUnavailable
from app.jobs.scheduler_state import SchedulerStateRepository
from app.stocks.universes.models import UniverseSelection
from app.config import Settings
from app.jobs.scheduler import build_scheduler_runtime
from backend.tests.jobs.test_scheduler_state import add_run
from types import SimpleNamespace


@pytest.fixture
def calendar():
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield SchedulerStateRepository(factory), factory
    engine.dispose()


def seed(factory, rows):
    with factory.begin() as session:
        session.add_all(TradeCalendar(exchange="SSE", cal_date=day, is_open=opened) for day, opened in rows)


@pytest.mark.parametrize("rows", [
    [(date(2026, 9, 18), True)],
    [(date(2026, 10, 9), True), (date(2026, 10, 11), False), (date(2026, 10, 12), False)],
])
def test_resolver_rejects_truncated_or_gapped_calendar(calendar, rows):
    repository, factory = calendar
    seed(factory, rows)
    with pytest.raises(TradeCalendarUnavailable):
        ExpectedBusinessDateResolver(repository, "Asia/Shanghai", 19, 0).resolve(datetime(2026, 10, 12, 12, tzinfo=UTC))


def test_closed_dates_are_required_and_resolve_last_open_without_guessing_weekdays(calendar):
    repository, factory = calendar
    seed(factory, [(date(2026, 10, 9), True), (date(2026, 10, 10), False),
                   (date(2026, 10, 11), False), (date(2026, 10, 12), False)])
    resolver = ExpectedBusinessDateResolver(repository, "Asia/Shanghai", 19, 0)
    assert resolver.resolve(datetime(2026, 10, 12, 12, tzinfo=UTC)) == date(2026, 10, 9)
    # 截止前仅要求昨天；不能额外要求尚未到期的今天记录。
    assert resolver.resolve(datetime(2026, 10, 13, 10, tzinfo=UTC)) == date(2026, 10, 9)


def test_startup_does_not_create_checkpoint_when_calendar_date_is_absent(calendar):
    repository, factory = calendar
    selection = UniverseSelection(profile="custom", version="v1", coverage="custom",
        symbols=("600519.SH",), fingerprint="test")
    with pytest.raises(TradeCalendarUnavailable):
        CatchupPlanner(repository, selection, max_days=10).plan_startup(
            date(2026, 10, 12), datetime(2026, 10, 12, 12, tzinfo=UTC))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SchedulerCheckpoint)) == 0


def test_checkpoint_window_gap_is_not_treated_as_zero_backlog(calendar):
    repository, factory = calendar
    seed(factory, [(date(2026, 10, 9), True), (date(2026, 10, 12), True)])
    selection = UniverseSelection(profile="custom", version="v1", coverage="custom",
        symbols=("600519.SH",), fingerprint="test")
    repository.ensure_checkpoint(selection, fallback=date(2026, 10, 9), now=datetime(2026, 10, 9, tzinfo=UTC))
    with pytest.raises(TradeCalendarUnavailable):
        CatchupPlanner(repository, selection, max_days=10).inspect(date(2026, 10, 12))


@pytest.mark.parametrize("complete", [False, True])
def test_actual_startup_preparation_checks_managed_window_before_creating_checkpoint(calendar, complete):
    repository, factory = calendar
    selection = UniverseSelection(profile="custom", version="v1", coverage="custom",
        symbols=("600519.SH",), fingerprint="test")
    seed(factory, [(date(2026, 10, 9), True), (date(2026, 10, 12), True)])
    if complete:
        seed(factory, [(date(2026, 10, 10), False), (date(2026, 10, 11), False)])
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    add_run(repository, selection, date(2026, 10, 9), "completed")
    runtime = build_scheduler_runtime(Settings(macro_sync_enabled=False),
        SimpleNamespace(session_factory=factory, selection=selection, job=None, mode="database"))
    # 实际接线的准备钩子，不启动调度器、任务或服务。
    result = runtime._prepare_checkpoint(now)
    if complete:
        assert result.managed_from == date(2026, 10, 9)
    else:
        assert result is False
        assert repository.checkpoint(selection) is None
