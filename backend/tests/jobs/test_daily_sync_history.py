from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.tushare.strategies import Dataset, TushareMode
from app.db.base import Base
from app.db.models import JobFailure, JobRun
from app.stocks.jobs.backfill import PlanFailure
from app.stocks.jobs.daily_sync import DailySyncExecutionContext
from app.stocks.jobs.daily_sync_history import DailySyncFinish, SqlDailySyncHistory
from app.stocks.universes.models import UniverseSelection


REPRESENTATIVE = UniverseSelection(
    profile="representative-50",
    version="2026-09-04-v1",
    coverage="representative",
    symbols=("600519.SH",),
    fingerprint="representative-fingerprint",
)
FULL_MARKET = UniverseSelection(
    profile="all",
    version="snapshot-abcd1234",
    coverage="full-market",
    symbols=("000001.SZ", "600519.SH"),
    fingerprint="full-market-fingerprint",
)


def make_history() -> tuple[Engine, sessionmaker[Session], SqlDailySyncHistory]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        execution_options={
            "schema_translate_map": {
                "raw": None,
                "core": None,
                "ops": None,
                "derived": None,
                "mart": None,
            }
        },
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return engine, factory, SqlDailySyncHistory(factory)


def test_start_writes_running_row_with_cli_execution_details() -> None:
    engine, factory, history = make_history()

    run_id = history.start(
        date(2026, 9, 2),
        TushareMode.STANDARD,
        REPRESENTATIVE,
    )

    with factory() as session:
        run = session.get(JobRun, run_id)
    assert run is not None
    assert UUID(run_id).version == 4
    assert run.job_name == "sync-daily"
    assert run.business_key == "2026-09-02:representative-50:2026-09-04-v1"
    assert run.status == "running"
    assert run.details == {
        "trigger": "cli",
        "universe": REPRESENTATIVE.to_payload(),
    }
    assert run.finished_at is None
    engine.dispose()


def test_finish_and_failure_persist_daily_run_details() -> None:
    engine, factory, history = make_history()
    run_id = history.start(
        date(2026, 9, 2),
        TushareMode.STANDARD,
        REPRESENTATIVE,
    )
    failure = PlanFailure(
        dataset=Dataset.DIVIDEND,
        request_key="dividend:600519.SH:20260902",
        error_code="provider_timeout",
        message="redacted provider failure",
        retryable=True,
        request_payload={"schemaVersion": 1, "dataset": "dividend"},
    )
    details: dict[str, object] = {
        "mode": "standard",
        "affectedSymbols": ["600519.SH"],
        "failedDatasets": ["dividend"],
        "version": "20260902-a1b2c3d4",
        "products": ["market-overview"],
        "quality": [],
    }
    finish = DailySyncFinish(
        status="partial",
        request_count=12,
        row_count=345,
        details=details,
        error_message="1 non-blocking dataset failed",
    )

    history.record_failure(run_id, failure)
    history.finish(run_id, finish)
    details["mode"] = "changed-after-finish"

    with factory() as session:
        run = session.get(JobRun, run_id)
        stored_failure = session.scalar(select(JobFailure))
    assert run is not None
    assert run.status == "partial"
    assert run.request_count == 12
    assert run.row_count == 345
    assert run.details["mode"] == "standard"
    assert run.details["affectedSymbols"] == ["600519.SH"]
    assert run.error_message == "1 non-blocking dataset failed"
    assert run.finished_at is not None
    assert stored_failure is not None
    assert stored_failure.job_run_id == run_id
    assert stored_failure.dataset == "dividend"
    assert stored_failure.request_key == "dividend:600519.SH:20260902"
    assert stored_failure.error_code == "provider_timeout"
    assert stored_failure.message == "redacted provider failure"
    assert stored_failure.retryable is True
    assert stored_failure.request_payload == {
        "schemaVersion": 1,
        "dataset": "dividend",
    }
    engine.dispose()


def test_scheduler_execution_context_is_preserved_in_history() -> None:
    engine, factory, history = make_history()
    execution = DailySyncExecutionContext(
        trigger="scheduler",
        scheduler_instance_id="instance-1",
    )

    run_id = history.start(
        date(2026, 9, 5),
        TushareMode.STANDARD,
        REPRESENTATIVE,
        execution=execution,
    )
    history.finish(
        run_id,
        DailySyncFinish(
            status="completed",
            request_count=1,
            row_count=2,
            details={
                "trigger": "cli",
                "universe": {"profile": "wrong"},
                "stage": "finished",
            },
        ),
    )

    with factory() as session:
        run = session.get(JobRun, run_id)
    assert run is not None
    assert run.details == {
        "trigger": "scheduler",
        "schedulerInstanceId": "instance-1",
        "universe": REPRESENTATIVE.to_payload(),
        "stage": "finished",
    }
    engine.dispose()


def test_scheduler_execution_requires_instance_id() -> None:
    with pytest.raises(
        ValueError,
        match="scheduler execution requires scheduler_instance_id",
    ):
        DailySyncExecutionContext(trigger="scheduler")


def test_has_completed_requires_exact_daily_completed_universe_row() -> None:
    engine, factory, history = make_history()
    with factory.begin() as session:
        session.add_all(
            (
                JobRun(
                    id="partial",
                    job_name="sync-daily",
                    business_key="2026-09-02:representative-50:2026-09-04-v1",
                    status="partial",
                ),
                JobRun(
                    id="failed",
                    job_name="sync-daily",
                    business_key="2026-09-02:representative-50:2026-09-04-v1",
                    status="failed",
                ),
                JobRun(
                    id="other-job",
                    job_name="backfill",
                    business_key="2026-09-02:representative-50:2026-09-04-v1",
                    status="completed",
                ),
                JobRun(
                    id="completed",
                    job_name="sync-daily",
                    business_key="2026-09-03:representative-50:2026-09-04-v1",
                    status="completed",
                ),
            )
        )

    assert history.has_completed(date(2026, 9, 2), REPRESENTATIVE) is False
    assert history.has_completed(date(2026, 9, 3), REPRESENTATIVE) is True
    assert history.has_completed(date(2026, 9, 3), FULL_MARKET) is False
    engine.dispose()


@pytest.mark.parametrize("status", ("skipped", "locked"))
def test_record_terminal_writes_finished_row_with_uuid(status: str) -> None:
    engine, factory, history = make_history()

    run_id = history.record_terminal(
        date(2026, 9, 2),
        TushareMode.STANDARD,
        REPRESENTATIVE,
        status=status,
        details={},
        execution=DailySyncExecutionContext(
            trigger="scheduler",
            scheduler_instance_id="instance-1",
        ),
    )

    with factory() as session:
        run = session.get(JobRun, run_id)
    assert run is not None
    assert UUID(run_id).version == 4
    assert run.job_name == "sync-daily"
    assert run.business_key == "2026-09-02:representative-50:2026-09-04-v1"
    assert run.status == status
    assert run.details == {
        "mode": "standard",
        "universe": REPRESENTATIVE.to_payload(),
        "trigger": "scheduler",
        "schedulerInstanceId": "instance-1",
    }
    assert run.finished_at is not None
    engine.dispose()


def test_job_run_default_details_remains_empty_for_old_callers() -> None:
    engine, factory, _history = make_history()
    with factory.begin() as session:
        session.add(
            JobRun(
                id="legacy-run",
                job_name="legacy",
                business_key="2026-09-02",
                status="completed",
            )
        )

    with factory() as session:
        run = session.get(JobRun, "legacy-run")
    assert run is not None
    assert run.details == {}
    engine.dispose()
