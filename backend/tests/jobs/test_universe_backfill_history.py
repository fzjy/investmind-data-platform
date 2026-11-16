from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.tushare.strategies import TushareMode
from app.db.models import JobRun
from app.stocks.jobs.universe_backfill import (
    UniverseBackfillCounts,
    UniverseBackfillOptions,
    UniverseBackfillPlan,
    UniverseBackfillPlanner,
)
from app.stocks.jobs.universe_backfill_history import (
    SqlUniverseBackfillHistory,
    UniverseBackfillResumeError,
)
from app.stocks.universes.models import UniverseSelection


NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)


@pytest.fixture
def factory(pg_factory):
    return pg_factory


@pytest.fixture
def plan() -> UniverseBackfillPlan:
    selection = UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("000001.SZ", "600519.SH"),
        fingerprint="selection-fingerprint",
    )
    return UniverseBackfillPlanner().build(
        UniverseBackfillOptions(
            as_of=date(2026, 9, 12),
            mode=TushareMode.STANDARD,
            selection=selection,
            batch_size=1,
        )
    )


def run(factory: sessionmaker[Session], run_id: str) -> JobRun:
    with factory() as session:
        stored = session.get(JobRun, run_id)
        assert stored is not None
        session.expunge(stored)
        return stored


def test_begin_creates_top_level_run_and_cursor_namespace(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    decision = SqlUniverseBackfillHistory(factory).begin(plan, NOW, force=False)

    assert decision.action == "start"
    assert decision.resumed is False
    assert decision.next_batch_index == 0
    assert decision.cursor_job_name == f"backfill-universe:{decision.run_id}"
    stored = run(factory, decision.run_id)
    assert stored.job_name == "backfill-universe"
    assert stored.business_key == plan.business_key
    assert stored.details["asOf"] == "2026-09-12"
    assert stored.details["mode"] == "standard"
    assert stored.details["marketStart"] == "2023-09-12"
    assert stored.details["financialStart"] == "2023-09-12"
    assert stored.details["batchSize"] == 1
    assert stored.details["batchCount"] == 2


def test_completed_run_is_skipped_without_force(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    first = history.begin(plan, NOW, force=False)
    history.finish(first.run_id, "completed", NOW, details={"version": "v1"})

    second = history.begin(plan, LATER, force=False)

    assert second.action == "skip"
    assert second.run_id == first.run_id


def test_completed_run_is_skipped_even_when_configuration_changed(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    first = history.begin(plan, NOW, force=False)
    history.finish(first.run_id, "completed", NOW, details={})

    decision = history.begin(
        replace(plan, configuration_fingerprint="changed"),
        LATER,
        force=False,
    )

    assert decision.action == "skip"
    assert decision.run_id == first.run_id


def test_failed_matching_run_resumes_same_checkpoint(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    first = history.begin(plan, NOW, force=False)
    history.checkpoint(
        first.run_id,
        next_batch_index=2,
        global_completed=True,
        current_dataset="daily",
        counts=UniverseBackfillCounts(row_count=20),
        now=NOW,
    )
    history.fail(first.run_id, "controlled failure", NOW)

    resumed = history.begin(plan, LATER, force=False)

    assert resumed.action == "resume"
    assert resumed.run_id == first.run_id
    assert resumed.next_batch_index == 2
    assert resumed.global_completed is True
    assert resumed.counts.row_count == 20
    stored = run(factory, first.run_id)
    assert stored.status == "running"
    assert stored.finished_at is None
    assert stored.error_message is None
    assert stored.details["attempt"] == 2


def test_changed_configuration_refuses_implicit_resume(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    history.begin(plan, NOW, force=False)
    changed = replace(
        plan,
        configuration_fingerprint="caller-secret-fingerprint-2",
    )

    with pytest.raises(UniverseBackfillResumeError, match="fingerprint") as error:
        history.begin(changed, LATER, force=False)

    assert plan.configuration_fingerprint not in str(error.value)
    assert changed.configuration_fingerprint not in str(error.value)


def test_force_supersedes_old_run_and_uses_new_cursor_namespace(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    first = history.begin(plan, NOW, force=False)

    forced = history.begin(plan, LATER, force=True)

    assert forced.action == "start"
    assert forced.run_id != first.run_id
    assert forced.cursor_job_name != first.cursor_job_name
    old = run(factory, first.run_id)
    assert old.status == "failed"
    assert old.details["supersededByForce"] is True
    with factory() as session:
        run_ids = tuple(session.scalars(select(JobRun.id).order_by(JobRun.id)))
    assert run_ids == tuple(sorted((first.run_id, forced.run_id)))


@pytest.mark.parametrize("terminal_status", ("completed", "partial"))
def test_force_preserves_terminal_status_when_finished_at_is_missing(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
    terminal_status: str,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    first = history.begin(plan, NOW, force=False)
    with factory.begin() as session:
        old = session.get(JobRun, first.run_id)
        assert old is not None
        old.status = terminal_status
        old.finished_at = None

    history.begin(plan, LATER, force=True)

    old = run(factory, first.run_id)
    assert old.status == terminal_status
    assert old.finished_at is None
    assert old.details["supersededByForce"] is True


def test_force_fails_running_status_even_when_finished_at_is_present(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    first = history.begin(plan, NOW, force=False)
    with factory.begin() as session:
        old = session.get(JobRun, first.run_id)
        assert old is not None
        old.finished_at = NOW

    history.begin(plan, LATER, force=True)

    old = run(factory, first.run_id)
    assert old.status == "failed"
    assert old.finished_at == LATER
    assert old.details["supersededByForce"] is True


def test_latest_run_uses_started_at_then_id_for_stable_resume_order(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    base_details = {
        "configurationFingerprint": plan.configuration_fingerprint,
        "selectionFingerprint": plan.options.selection.fingerprint,
        "universeProfile": plan.options.selection.profile,
        "universeVersion": plan.options.selection.version,
        "coverage": plan.options.selection.coverage,
        "universeSize": len(plan.options.selection.symbols),
        "globalCompleted": False,
        "nextBatchIndex": 0,
        "completedBatchCount": 0,
        "currentDataset": None,
        "counts": UniverseBackfillCounts().to_payload(),
        "attempt": 1,
    }
    with factory.begin() as session:
        session.add_all(
            (
                JobRun(
                    id="a-earlier",
                    job_name="backfill-universe",
                    business_key=plan.business_key,
                    status="failed",
                    started_at=NOW,
                    details={
                        **base_details,
                        "cursorJobName": "backfill-universe:a-earlier",
                    },
                ),
                JobRun(
                    id="z-latest",
                    job_name="backfill-universe",
                    business_key=plan.business_key,
                    status="failed",
                    started_at=NOW,
                    details={
                        **base_details,
                        "cursorJobName": "backfill-universe:z-latest",
                        "nextBatchIndex": 1,
                    },
                ),
            )
        )

    decision = SqlUniverseBackfillHistory(factory).begin(plan, LATER, force=False)

    assert decision.action == "resume"
    assert decision.run_id == "z-latest"
    assert decision.next_batch_index == 1


def test_checkpoint_replaces_details_with_complete_progress_payload(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    decision = history.begin(plan, NOW, force=False)
    counts = UniverseBackfillCounts(
        provider_count=30,
        retained_count=25,
        filtered_count=5,
        row_count=24,
        rejected_count=1,
        request_count=4,
    )

    history.checkpoint(
        decision.run_id,
        next_batch_index=2,
        global_completed=True,
        current_dataset="daily",
        counts=counts,
        now=LATER,
    )

    stored = run(factory, decision.run_id)
    assert stored.details == {
        "asOf": "2026-09-12",
        "mode": "standard",
        "marketStart": "2023-09-12",
        "financialStart": "2023-09-12",
        "batchSize": 1,
        "batchCount": 2,
        "configurationFingerprint": plan.configuration_fingerprint,
        "selectionFingerprint": plan.options.selection.fingerprint,
        "universeProfile": plan.options.selection.profile,
        "universeVersion": plan.options.selection.version,
        "coverage": plan.options.selection.coverage,
        "universeSize": len(plan.options.selection.symbols),
        "cursorJobName": f"backfill-universe:{decision.run_id}",
        "globalCompleted": True,
        "nextBatchIndex": 2,
        "completedBatchCount": 2,
        "currentDataset": "daily",
        "counts": counts.to_payload(),
        "attempt": 1,
    }
    assert stored.request_count == 4
    assert stored.row_count == 24


def test_finish_sets_terminal_state_and_copies_details(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    decision = history.begin(plan, NOW, force=False)
    history.checkpoint(
        decision.run_id,
        next_batch_index=1,
        global_completed=True,
        current_dataset="daily",
        counts=UniverseBackfillCounts(request_count=3, row_count=12),
        now=NOW,
    )
    details: dict[str, object] = {"publish": {"version": "v1"}}

    history.finish(decision.run_id, "partial", LATER, details=details)
    details["publish"] = {"version": "changed"}

    stored = run(factory, decision.run_id)
    assert stored.status == "partial"
    assert stored.finished_at is not None
    assert stored.details["publish"] == {"version": "v1"}
    assert stored.details["asOf"] == "2026-09-12"
    assert stored.details["mode"] == "standard"
    assert stored.details["marketStart"] == "2023-09-12"
    assert stored.details["financialStart"] == "2023-09-12"
    assert stored.details["batchSize"] == 1
    assert stored.details["batchCount"] == 2
    assert stored.details["nextBatchIndex"] == 1
    assert stored.request_count == 3
    assert stored.row_count == 12


def test_finish_rejects_unknown_terminal_status(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    decision = history.begin(plan, NOW, force=False)

    with pytest.raises(ValueError, match="completed, partial, or failed"):
        history.finish(
            decision.run_id,
            "skipped",  # type: ignore[arg-type]
            LATER,
            details={},
        )

    assert run(factory, decision.run_id).status == "running"


def test_fail_persists_caller_sanitized_error_without_rewriting_it(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    decision = history.begin(plan, NOW, force=False)

    history.fail(decision.run_id, "sanitized provider failure", LATER)

    stored = run(factory, decision.run_id)
    assert stored.status == "failed"
    assert stored.error_message == "sanitized provider failure"
    assert stored.finished_at is not None


def test_all_writes_reject_naive_datetime(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    history = SqlUniverseBackfillHistory(factory)
    naive = NOW.replace(tzinfo=None)

    with pytest.raises(ValueError, match="now must be timezone-aware"):
        history.begin(plan, naive, force=False)

    decision = history.begin(plan, NOW, force=False)
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        history.checkpoint(
            decision.run_id,
            next_batch_index=0,
            global_completed=False,
            current_dataset=None,
            counts=UniverseBackfillCounts(),
            now=naive,
        )
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        history.finish(decision.run_id, "completed", naive, details={})
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        history.fail(decision.run_id, "sanitized", naive)


def test_decision_is_immutable(
    factory: sessionmaker[Session],
    plan: UniverseBackfillPlan,
) -> None:
    decision = SqlUniverseBackfillHistory(factory).begin(plan, NOW, force=False)

    with pytest.raises(FrozenInstanceError):
        decision.action = "skip"  # type: ignore[misc]
