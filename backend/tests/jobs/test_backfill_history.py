from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.connectors.tushare.errors import TushareTransientError
from app.connectors.tushare.strategies import Dataset, TushareMode
from app.db.base import Base
from app.db.models import JobFailure, JobRun
from app.ingestion.pagination import PaginatedPlanRunner, PaginatedResult
from app.ingestion.pipeline import BatchResult
from app.stocks.jobs.backfill import BackfillJob, BackfillOptions, BackfillReport


def make_batch_result(*, batch_id: str, received_count: int, rejected_count: int = 0) -> BatchResult:
    return BatchResult(
        batch_id=batch_id,
        dataset=Dataset.SECURITY,
        received_count=received_count,
        row_count=received_count - rejected_count,
        rejected_count=rejected_count,
    )


def test_sql_history_persists_failed_request() -> None:
    from app.stocks.jobs.backfill_history import SqlBackfillHistory

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

    def fail(_plan) -> None:
        raise TushareTransientError("temporary provider outage")

    report = BackfillJob(fail, history=SqlBackfillHistory(factory)).run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    with factory() as session:
        run = session.scalar(select(JobRun))
        failure = session.scalar(select(JobFailure))

    assert run is not None
    assert run.id == report.job_run_id
    assert run.job_name == "backfill"
    assert run.business_key == "2026-08-17:standard"
    assert run.status == "failed"
    assert run.request_count == 0
    assert run.row_count == 0
    assert run.finished_at is not None
    assert failure is not None
    assert failure.job_run_id == run.id
    assert failure.dataset == "security"
    assert failure.request_key == "security:all"
    assert failure.error_code == "TushareTransientError"
    assert failure.message == "temporary provider outage"
    assert failure.retryable is True
    assert failure.request_payload == {
        "schemaVersion": 1,
        "mode": "standard",
        "dataset": "security",
        "apiName": "stock_basic",
        "params": {"list_status": "L"},
        "fields": [
            "ts_code",
            "symbol",
            "name",
            "exchange",
            "list_date",
            "market",
            "list_status",
        ],
        "cursorKey": "security:all",
        "pageSize": 5000,
    }
    engine.dispose()


def test_sql_history_marks_started_run_failed() -> None:
    from app.stocks.jobs.backfill_history import SqlBackfillHistory

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
    history = SqlBackfillHistory(factory)
    run_id = history.start(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
    )

    history.fail(run_id, "history finalization failed")

    with factory() as session:
        run = session.get(JobRun, run_id)

    assert run is not None
    assert run.status == "failed"
    assert run.error_message == "history finalization failed"
    assert run.finished_at is not None
    engine.dispose()


def test_sql_history_persists_actual_request_counts_from_results() -> None:
    from app.stocks.jobs.backfill_history import SqlBackfillHistory

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
    history = SqlBackfillHistory(factory)
    run_id = history.start(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
    )
    partial = PaginatedResult(
        dataset=Dataset.SECURITY,
        request_key="security:all",
        pages=2,
        request_count=2,
        resumed_from_offset=0,
        batches=(
            make_batch_result(batch_id="batch-1", received_count=3),
            make_batch_result(batch_id="batch-2", received_count=1),
        ),
        completed=False,
    )
    legacy = type("LegacyResult", (), {"row_count": 5})()
    report = BackfillReport(
        planned=2,
        completed=1,
        failed=0,
        results=(partial, legacy),
    )
    history.finish(run_id, report)

    with factory() as session:
        run = session.scalar(select(JobRun))

    assert run is not None
    assert run.id == run_id
    assert run.status == "completed"
    assert run.request_count == 3
    assert run.row_count == 9
    assert run.error_message is None
    engine.dispose()


def test_sql_history_persists_second_page_provider_attempt_from_partial_result() -> None:
    from app.stocks.jobs.backfill_history import SqlBackfillHistory

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

    class MemoryCursorStore:
        def __init__(self) -> None:
            self.value: dict[str, object] | None = None

        def load(self, _cursor_key: str) -> dict[str, object] | None:
            return self.value

        def save(self, _cursor_key: str, cursor_value: dict[str, object]) -> None:
            self.value = dict(cursor_value)

        def delete(self, _cursor_key: str) -> None:
            self.value = None

    def run_page(page_plan):
        if page_plan.params["offset"] == "0":
            return make_batch_result(batch_id="batch-1", received_count=2)
        raise TushareTransientError("temporary provider outage")

    paginated_runner = PaginatedPlanRunner(
        run_page,
        MemoryCursorStore(),
        mode=TushareMode.STANDARD,
        page_size=2,
    )
    report = BackfillJob(paginated_runner, history=SqlBackfillHistory(factory)).run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    with factory() as session:
        run = session.scalar(select(JobRun))
        failure = session.scalar(select(JobFailure))

    assert report.failed == 1
    assert report.completed == 0
    assert len(report.results) == 1
    assert report.results[0].pages == 1
    assert report.results[0].request_count == 2
    assert run is not None
    assert run.request_count == 2
    assert failure is not None
    assert failure.request_payload["pageSize"] == 2
    engine.dispose()


def test_sql_history_counts_zero_requests_for_failed_plan_without_partial_result() -> None:
    from app.stocks.jobs.backfill_history import SqlBackfillHistory

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

    def fail(_plan) -> None:
        raise TushareTransientError("temporary provider outage")

    report = BackfillJob(fail, history=SqlBackfillHistory(factory)).run(
        BackfillOptions(date(2026, 8, 17)),
        TushareMode.STANDARD,
        (Dataset.SECURITY,),
    )

    with factory() as session:
        run = session.scalar(select(JobRun))

    assert run is not None
    assert run.id == report.job_run_id
    assert run.request_count == 0
    assert run.row_count == 0
    assert run.status == "failed"
    engine.dispose()
