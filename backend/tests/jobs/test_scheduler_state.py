from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import JobFailure, JobRun, TradeCalendar
from app.stocks.jobs.daily_sync_history import daily_sync_business_key
from app.jobs.scheduler_state import SchedulerStateRepository
from app.stocks.universes.models import UniverseSelection


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
RECOVERY_NOW = NOW.replace(tzinfo=UTC)
LATER = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
RESOLVED = datetime(2026, 9, 5, 14, 0, tzinfo=UTC)


def dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 5, hour, minute, tzinfo=UTC)


@pytest.fixture
def selection() -> UniverseSelection:
    return UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="representative-fingerprint",
    )


@pytest.fixture
def other_selection() -> UniverseSelection:
    return UniverseSelection(
        profile="custom",
        version="2026-09-04-v1",
        coverage="custom",
        symbols=("000001.SZ",),
        fingerprint="custom-fingerprint",
    )


@pytest.fixture
def repository(pg_factory) -> Iterator[SchedulerStateRepository]:
    engine, factory = pg_factory.kw["bind"], pg_factory
    yield SchedulerStateRepository(factory)
    engine.dispose()


def factory_for(repository: SchedulerStateRepository) -> sessionmaker[Session]:
    return repository._session_factory


def add_run(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    business_date: date,
    status: str,
    *,
    run_id: str | None = None,
    started_at: datetime = NOW,
    finished_at: datetime | None = LATER,
    job_name: str = "sync-daily",
    business_key: str | None = None,
    details: dict[str, object] | None = None,
) -> str:
    identifier = run_id or f"{business_date.isoformat()}-{status}-{job_name}"
    with factory_for(repository).begin() as session:
        session.add(
            JobRun(
                id=identifier,
                job_name=job_name,
                business_key=business_key or daily_sync_business_key(business_date, selection),
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                details=details or {},
            )
        )
    return identifier


def test_heartbeat_lifecycle_keeps_old_instances_and_returns_latest(
    repository: SchedulerStateRepository,
) -> None:
    first = repository.start_instance("daily-sync", now=dt(10, 0), details={})
    second = repository.start_instance("daily-sync", now=dt(11, 0), details={})

    touched = repository.touch_instance(
        second,
        now=dt(11, 1),
        next_run_at=dt(19, 0),
        state="running",
    )

    latest = repository.latest_instance("daily-sync")
    assert latest is not None
    assert latest.instance_id == second
    assert latest.next_run_at == dt(19, 0)
    assert touched == latest
    assert first != second
    assert repository.heartbeat(first) is not None


def test_heartbeat_returns_defensive_json_copy_and_stop_marks_stopping(
    repository: SchedulerStateRepository,
) -> None:
    instance_id = repository.start_instance(
        "daily-sync",
        now=NOW,
        details={"nested": {"count": 1}},
    )

    first = repository.heartbeat(instance_id)
    assert first is not None
    first.details["nested"] = {"count": 99}
    repository.stop_instance(instance_id, now=LATER)
    stopped = repository.heartbeat(instance_id)

    assert stopped is not None
    assert stopped.state == "stopping"
    assert stopped.last_heartbeat_at == LATER
    assert stopped.details == {"nested": {"count": 1}}


def test_checkpoint_never_moves_forward_implicitly(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    first = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 1), now=NOW)
    second = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 5), now=NOW)

    assert first.managed_from == second.managed_from == date(2026, 9, 1)
    assert second.key == "sync-daily:representative-50:2026-09-04-v1"


def test_checkpoint_uses_latest_completed_run_before_fallback(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(repository, selection, date(2026, 9, 2), "completed")
    add_run(repository, selection, date(2026, 9, 3), "failed")

    checkpoint = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 5), now=NOW)

    assert checkpoint.managed_from == date(2026, 9, 2)


def test_checkpoint_uses_stable_completed_order_and_copies_evaluation_details(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(
        repository,
        selection,
        date(2026, 9, 2),
        "completed",
        run_id="earlier-finished",
        started_at=dt(8, 0),
        finished_at=dt(9, 0),
    )
    add_run(
        repository,
        selection,
        date(2026, 9, 3),
        "completed",
        run_id="later-started",
        started_at=dt(10, 0),
        finished_at=dt(9, 0),
    )
    checkpoint = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 5), now=NOW)
    details: dict[str, object] = {"missing": ["2026-09-04"]}

    repository.update_checkpoint_evaluation(checkpoint.key, now=LATER, details=details)
    details["missing"] = []
    viewed = repository.checkpoint(selection)

    assert checkpoint.managed_from == date(2026, 9, 3)
    assert viewed is not None
    assert viewed.last_evaluated_at == LATER
    assert viewed.details == {"missing": ["2026-09-04"]}


def test_checkpoint_uses_started_at_after_equal_effective_finished_at(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(
        repository,
        selection,
        date(2026, 9, 4),
        "completed",
        run_id="later-start-earlier-business-date",
        started_at=dt(10, 0),
        finished_at=dt(12, 0),
    )
    add_run(
        repository,
        selection,
        date(2026, 9, 5),
        "completed",
        run_id="earlier-start-later-business-date",
        started_at=dt(9, 0),
        finished_at=dt(12, 0),
    )

    checkpoint = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 6), now=NOW)

    assert checkpoint.managed_from == date(2026, 9, 4)


def test_checkpoint_race_re_reads_existing_stable_key(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 2), now=NOW)
    original_get = Session.get
    hidden_once = True

    def hide_existing_checkpoint(
        session: Session,
        entity: object,
        ident: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal hidden_once
        if entity is not None and ident == existing.key and hidden_once:
            hidden_once = False
            return None
        return original_get(session, entity, ident, *args, **kwargs)

    monkeypatch.setattr(Session, "get", hide_existing_checkpoint)

    result = repository.ensure_checkpoint(selection, fallback=date(2026, 9, 5), now=LATER)

    assert result == existing


def test_calendar_queries_use_sse_and_sort_open_dates(
    repository: SchedulerStateRepository,
) -> None:
    with factory_for(repository).begin() as session:
        session.add_all(
            (
                TradeCalendar(exchange="SSE", cal_date=date(2026, 9, 1), is_open=True),
                TradeCalendar(exchange="SSE", cal_date=date(2026, 9, 2), is_open=False),
                TradeCalendar(exchange="SSE", cal_date=date(2026, 9, 3), is_open=True),
                TradeCalendar(exchange="SZSE", cal_date=date(2026, 9, 4), is_open=True),
            )
        )

    assert repository.latest_open_date(on_or_before=date(2026, 9, 2)) == date(2026, 9, 1)
    assert repository.trading_day(date(2026, 9, 1)) is True
    assert repository.trading_day(date(2026, 9, 2)) is False
    assert repository.trading_day(date(2026, 9, 4)) is None
    assert repository.open_dates(start=date(2026, 9, 1), end=date(2026, 9, 4)) == (
        date(2026, 9, 1),
        date(2026, 9, 3),
    )


def test_completed_dates_requires_exact_completed_daily_business_key(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    other_selection: UniverseSelection,
) -> None:
    completed = date(2026, 9, 1)
    requested_but_failed = date(2026, 9, 2)
    wrong_job = date(2026, 9, 3)
    other_universe = date(2026, 9, 4)
    add_run(repository, selection, completed, "completed")
    add_run(repository, selection, requested_but_failed, "failed")
    add_run(repository, selection, wrong_job, "completed", job_name="backfill")
    add_run(repository, other_selection, other_universe, "completed")

    assert repository.completed_dates(
        selection,
        (completed, requested_but_failed, wrong_job, other_universe),
    ) == frozenset({completed})
    assert repository.completed_dates(selection, ()) == frozenset()


def test_latest_daily_run_and_consecutive_failures_ignore_nonterminal_states(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(repository, selection, date(2026, 9, 1), "completed", finished_at=dt(9, 0))
    add_run(repository, selection, date(2026, 9, 2), "failed", finished_at=dt(10, 0))
    add_run(repository, selection, date(2026, 9, 3), "locked", finished_at=dt(11, 0))
    add_run(repository, selection, date(2026, 9, 4), "failed", finished_at=dt(12, 0))
    add_run(
        repository,
        selection,
        date(2026, 9, 5),
        "running",
        finished_at=None,
        started_at=dt(13, 0),
    )

    latest = repository.latest_daily_run(selection)

    assert latest is not None
    assert latest.business_date == date(2026, 9, 5)
    assert latest.status == "running"
    relevant = repository.latest_daily_run(
        selection,
        statuses=("failed", "completed", "partial"),
    )
    assert relevant is not None
    assert relevant.business_date == date(2026, 9, 4)
    assert relevant.status == "failed"
    assert repository.consecutive_failures(selection) == 2


def test_consecutive_failures_ignores_skipped_runs(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(repository, selection, date(2026, 9, 1), "completed", finished_at=dt(9, 0))
    add_run(repository, selection, date(2026, 9, 2), "failed", finished_at=dt(10, 0))
    add_run(repository, selection, date(2026, 9, 3), "skipped", finished_at=dt(11, 0))
    add_run(repository, selection, date(2026, 9, 4), "failed", finished_at=dt(12, 0))

    assert repository.consecutive_failures(selection) == 2


def test_consecutive_failures_partial_run_resets_history(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(repository, selection, date(2026, 9, 1), "failed", finished_at=dt(9, 0))
    add_run(repository, selection, date(2026, 9, 2), "failed", finished_at=dt(10, 0))
    add_run(repository, selection, date(2026, 9, 3), "partial", finished_at=dt(11, 0))

    assert repository.consecutive_failures(selection) == 0


def test_latest_daily_run_details_are_a_defensive_copy(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    add_run(
        repository,
        selection,
        date(2026, 9, 5),
        "completed",
        details={"nested": {"count": 1}},
    )

    first = repository.latest_daily_run(selection)
    assert first is not None
    nested = first.details["nested"]
    assert isinstance(nested, dict)
    nested["count"] = 99
    second = repository.latest_daily_run(selection)

    assert second is not None
    assert second.details == {"nested": {"count": 1}}


def test_recover_interrupted_runs_only_marks_offline_scheduler_work_failed(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
    other_selection: UniverseSelection,
) -> None:
    offline = repository.start_instance("daily-sync", now=dt(10, 0), details={})
    online = repository.start_instance("daily-sync", now=dt(11, 57), details={})
    stopping = repository.start_instance("daily-sync", now=dt(11, 58), details={})
    foreign = repository.start_instance("other-scheduler", now=dt(10, 0), details={})
    repository.stop_instance(stopping, now=dt(11, 59))
    interrupted_id = add_run(
        repository,
        selection,
        date(2026, 9, 1),
        "running",
        run_id="offline-scheduler-run",
        finished_at=None,
        details={"trigger": "scheduler", "schedulerInstanceId": offline},
    )
    stopping_id = add_run(
        repository,
        selection,
        date(2026, 9, 2),
        "running",
        run_id="stopping-scheduler-run",
        finished_at=None,
        details={"trigger": "scheduler", "schedulerInstanceId": stopping},
    )
    online_id = add_run(
        repository,
        selection,
        date(2026, 9, 3),
        "running",
        run_id="online-scheduler-run",
        finished_at=None,
        details={"trigger": "scheduler", "schedulerInstanceId": online},
    )
    cli_id = add_run(
        repository,
        selection,
        date(2026, 9, 4),
        "running",
        run_id="cli-run",
        finished_at=None,
        details={"trigger": "cli"},
    )
    unowned_id = add_run(
        repository,
        selection,
        date(2026, 9, 5),
        "running",
        run_id="unowned-scheduler-run",
        finished_at=None,
        details={"trigger": "scheduler"},
    )
    other_id = add_run(
        repository,
        other_selection,
        date(2026, 9, 1),
        "running",
        run_id="other-universe-run",
        finished_at=None,
        details={"trigger": "scheduler", "schedulerInstanceId": offline},
    )
    foreign_id = add_run(
        repository,
        selection,
        date(2026, 9, 6),
        "running",
        run_id="foreign-scheduler-run",
        finished_at=None,
        details={"trigger": "scheduler", "schedulerInstanceId": foreign},
    )

    recovered = repository.recover_interrupted_runs(
        selection,
        now=RECOVERY_NOW,
        timeout=timedelta(minutes=3),
    )

    assert recovered == (interrupted_id, stopping_id)
    with factory_for(repository)() as session:
        recovered_runs = {
            run_id: session.get(JobRun, run_id)
            for run_id in (interrupted_id, stopping_id)
        }
        untouched_runs = {
            run_id: session.get(JobRun, run_id)
            for run_id in (online_id, cli_id, unowned_id, other_id, foreign_id)
        }
        failures = session.query(JobFailure).order_by(JobFailure.job_run_id).all()

    assert all(run is not None and run.status == "failed" for run in recovered_runs.values())
    assert all(run is not None and run.finished_at == NOW for run in recovered_runs.values())
    assert all(
        run is not None and run.details["errorCode"] == "SCHEDULER_INTERRUPTED"
        for run in recovered_runs.values()
    )
    assert all(
        run is not None
        and run.error_message == "scheduler stopped before the run finished"
        for run in recovered_runs.values()
    )
    assert all(run is not None and run.status == "running" for run in untouched_runs.values())
    assert [failure.job_run_id for failure in failures] == sorted(recovered)
    assert all(failure.dataset == "scheduler" for failure in failures)
    assert all(failure.error_code == "SCHEDULER_INTERRUPTED" for failure in failures)
    assert all(failure.retryable is True for failure in failures)

    assert repository.recover_interrupted_runs(
        selection,
        now=RECOVERY_NOW,
        timeout=timedelta(minutes=3),
    ) == ()
    with factory_for(repository)() as session:
        assert session.query(JobFailure).count() == 2


def test_recover_interrupted_runs_rejects_naive_now(
    repository: SchedulerStateRepository,
    selection: UniverseSelection,
) -> None:
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        repository.recover_interrupted_runs(
            selection,
            now=NOW.replace(tzinfo=None),
            timeout=timedelta(minutes=3),
        )


def test_open_alert_is_idempotent_and_resolvable(
    repository: SchedulerStateRepository,
) -> None:
    one = repository.open_alert("key", "catchup-backlog", "warning", NOW, {"count": 12})
    two = repository.open_alert("key", "catchup-backlog", "warning", LATER, {"count": 11})

    assert one.id == two.id
    assert two.details == {"count": 11}
    assert two.first_triggered_at == NOW
    assert two.last_triggered_at == LATER
    repository.resolve_alert("key", RESOLVED)
    resolved = repository.alert("key")
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolved_at == RESOLVED


def test_open_alert_race_re_reads_existing_stable_key(
    repository: SchedulerStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = repository.open_alert("key", "catchup-backlog", "warning", NOW, {"count": 1})
    original_scalar = Session.scalar
    hidden_once = True

    def hide_existing_alert(
        session: Session,
        statement: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal hidden_once
        if hidden_once:
            hidden_once = False
            return None
        return original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", hide_existing_alert)

    result = repository.open_alert("key", "catchup-backlog", "warning", LATER, {"count": 2})

    assert result.id == existing.id
    assert result.last_triggered_at == LATER
    assert result.details == {"count": 2}


def test_alert_reopens_same_row_and_open_alerts_are_defensive_and_sorted(
    repository: SchedulerStateRepository,
) -> None:
    original = repository.open_alert("z-key", "catchup-backlog", "warning", NOW, {"count": 1})
    repository.resolve_alert("z-key", LATER)
    reopened = repository.open_alert("z-key", "catchup-backlog", "warning", RESOLVED, {"count": 2})
    repository.open_alert("a-key", "trade-calendar", "critical", NOW, {"nested": {"value": 1}})

    alerts = repository.open_alerts()
    assert reopened.id == original.id
    assert reopened.status == "open"
    assert reopened.first_triggered_at == RESOLVED
    assert reopened.resolved_at is None
    assert tuple(alert.key for alert in alerts) == ("a-key", "z-key")
    alerts[0].details["nested"] = {"value": 99}
    assert repository.alert("a-key").details == {"nested": {"value": 1}}

@pytest.mark.parametrize("attributes,constraint,expected", [
    ({"pgcode": "23505"}, "scheduler_checkpoint_pkey", True),
    ({"sqlstate": "23505"}, "scheduler_checkpoint_pkey", True),
    ({"pgcode": "23505"}, "another_constraint", False),
    ({"pgcode": "23503"}, "scheduler_checkpoint_pkey", False),
    ({"sqlstate": "23503", "pgcode": "23505"}, "scheduler_checkpoint_pkey", False),
])
def test_expected_duplicate_key_driver_diagnostics(attributes, constraint, expected):
    from types import SimpleNamespace
    from sqlalchemy.exc import IntegrityError
    from app.jobs.scheduler_state import _is_expected_duplicate_key

    original = Exception("database error")
    for name, value in attributes.items():
        setattr(original, name, value)
    original.diag = SimpleNamespace(constraint_name=constraint)
    error = IntegrityError(None, None, original)
    assert _is_expected_duplicate_key(
        error, postgresql_constraints=frozenset({"scheduler_checkpoint_pkey"})
    ) is expected
