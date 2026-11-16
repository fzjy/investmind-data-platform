from __future__ import annotations

from importlib import import_module

import pytest
from sqlalchemy import create_engine, inspect

from app.db.base import Base, MartBase
from app.db.models import (
    MacroSeriesState,
    SchedulerCheckpoint,
    SchedulerHeartbeat,
    OperationalAlert,
    PublishedSnapshot,
    core,
    mart,
    ops,
    raw,
)  # noqa: F401
from app.db.models.derived import RiskResult


def test_metadata_registers_foundation_tables() -> None:
    table_names = set(Base.metadata.tables)

    assert "raw.ingestion_batch" in table_names
    assert "raw.source_payload" in table_names
    assert "core.security" in table_names
    assert "core.daily_price" in table_names
    assert "core.income_statement" in table_names
    assert "ops.job_run" in table_names
    assert "ops.job_cursor" in table_names


def test_target_evidence_owned_by_later_separate_metadata():
    from app.db.base import ALL_BASES, TargetEvidenceBase
    from app.db.models import TargetObservation, TargetCandidate
    names = {'raw.enterprise_target_observation', 'core.enterprise_target_candidate',
        'ops.enterprise_target_execution_plan', 'ops.enterprise_target_execution_attempt'}
    assert set(TargetEvidenceBase.metadata.tables) == names
    assert not names & set(Base.metadata.tables) and TargetEvidenceBase in ALL_BASES
    assert TargetObservation.metadata is TargetEvidenceBase.metadata
    assert TargetCandidate.metadata is TargetEvidenceBase.metadata


def test_macro_series_state_is_registered() -> None:
    assert "ops.macro_series_state" in Base.metadata.tables
    assert set(MacroSeriesState.__table__.c.keys()) == {
        "series_id",
        "catalog_version",
        "last_checked_at",
        "last_success_at",
        "latest_observed_at",
        "latest_released_at",
        "active_source",
        "fallback_used",
        "stale",
        "stale_reason",
        "last_error_code",
        "last_error_message",
        "consecutive_failures",
        "updated_at",
    }
    assert {
        name: str(MacroSeriesState.__table__.c[name].server_default.arg)
        for name in ("fallback_used", "stale", "consecutive_failures")
    } == {
        "fallback_used": "false",
        "stale": "false",
        "consecutive_failures": "0",
    }


@pytest.mark.parametrize("table_exists", [False, True])
def test_macro_series_state_migration_creates_only_missing_table(
    monkeypatch: pytest.MonkeyPatch,
    table_exists: bool,
) -> None:
    migration = import_module(
        "migrations.versions.20260920_0009_macro_series_state"
    )
    created: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeInspector:
        def has_table(self, table_name: str, *, schema: str) -> bool:
            assert table_name == "macro_series_state"
            assert schema == "ops"
            return table_exists

    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: FakeInspector())
    monkeypatch.setattr(migration.op, "get_bind", object)
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    migration.upgrade()

    assert bool(created) is not table_exists
    if created:
        args, kwargs = created[0]
        assert args[0] == "macro_series_state"
        assert kwargs == {"schema": "ops"}


@pytest.mark.parametrize("table_exists", [False, True])
def test_macro_series_state_migration_drops_only_existing_table(
    monkeypatch: pytest.MonkeyPatch,
    table_exists: bool,
) -> None:
    migration = import_module(
        "migrations.versions.20260920_0009_macro_series_state"
    )
    dropped: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeInspector:
        def has_table(self, table_name: str, *, schema: str) -> bool:
            assert table_name == "macro_series_state"
            assert schema == "ops"
            return table_exists

    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: FakeInspector())
    monkeypatch.setattr(migration.op, "get_bind", object)
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )

    migration.downgrade()

    assert dropped == (
        [(('macro_series_state',), {"schema": "ops"})]
        if table_exists
        else []
    )


def test_risk_result_has_idempotent_identity() -> None:
    names = {constraint.name for constraint in RiskResult.__table__.constraints}

    assert "uq_risk_result_security_rule_date" in names


@pytest.mark.parametrize("constraint_exists", [False, True])
def test_risk_identity_migration_creates_expected_constraint_only_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    constraint_exists: bool,
) -> None:
    migration = import_module(
        "migrations.versions.20260825_0006_derived_risk_identity"
    )
    created: list[tuple[tuple[object, ...], dict[str, object]]] = []
    executed: list[str] = []
    constraints = (
        [{"name": "uq_risk_result_security_rule_date"}]
        if constraint_exists
        else []
    )

    class FakeInspector:
        def get_unique_constraints(
            self,
            table_name: str,
            *,
            schema: str,
        ) -> list[dict[str, str]]:
            assert table_name == "risk_result"
            assert schema == "derived"
            return constraints

    monkeypatch.setattr(migration, "inspect", lambda bind: FakeInspector(), raising=False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.op,
        "create_unique_constraint",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: executed.append(str(statement)),
    )

    migration.upgrade()

    if constraint_exists:
        assert created == []
    else:
        assert created == [
            (
                (
                    "uq_risk_result_security_rule_date",
                    "risk_result",
                    ["ts_code", "risk_id", "as_of"],
                ),
                {"schema": "derived"},
            )
        ]
    assert len(executed) == 1
    assert "ROW_NUMBER() OVER" in executed[0]
    assert "PARTITION BY ts_code, risk_id, as_of" in executed[0]


def test_risk_identity_migration_downgrade_preserves_existing_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module(
        "migrations.versions.20260825_0006_derived_risk_identity"
    )
    dropped: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )

    migration.downgrade()

    assert dropped == []


def test_foundation_tables_exist_in_postgres(pg_factory) -> None:
    engine = pg_factory.kw["bind"]

    assert {"local_admin_task", "local_admin_execution"} <= set(inspect(engine).get_table_names(schema="ops"))
    assert all(inspect(engine).has_table(t.name, schema=t.schema) for t in Base.metadata.tables.values())
    assert set(Base.metadata.tables) <= {
        "ops.local_admin_task",
        "ops.local_admin_execution",
        "ops.data_acquisition_state",
        "raw.ingestion_batch",
        "raw.source_payload",
        "raw.rejected_record",
        "raw.enterprise_receivable_response",
        "raw.enterprise_order_response",
        "raw.enterprise_rea_response",
        "core.security",
        "core.security_name_history",
        "core.listing_status_history",
        "core.trade_calendar",
        "core.classification",
        "core.classification_member",
        "core.index_constituent",
        "core.daily_price",
        "core.market_daily_quote",
        "core.enterprise_receivable_supplement",
        "core.enterprise_order_supplement",
        "core.enterprise_rea_supplement",
        "core.daily_basic",
        "core.adjustment_factor",
        "core.price_limit",
        "core.suspension",
        "core.income_statement",
        "core.balance_sheet",
        "core.cashflow_statement",
        "core.financial_indicator",
        "core.financial_forecast",
        "core.financial_express",
        "core.dividend",
        "core.repurchase",
        "core.share_unlock",
        "core.disclosure_schedule",
        "core.shareholder_count",
        "core.top_holder",
        "core.business_segment",
        "ops.job_run",
        "ops.news_delivery",
        "ops.enterprise_receivable_attempt",
        "ops.enterprise_order_attempt",
        "ops.enterprise_rea_plan",
        "ops.enterprise_rea_attempt",
        "ops.job_cursor",
        "ops.job_failure",
        "ops.quality_check_result",
        "ops.scheduler_heartbeat",
        "ops.scheduler_checkpoint",
        "ops.operational_alert",
        "ops.macro_series_state",
    }


def test_scheduler_operations_tables_exist_in_postgres(pg_factory) -> None:
    assert {
        SchedulerHeartbeat.__table__.fullname,
        SchedulerCheckpoint.__table__.fullname,
        OperationalAlert.__table__.fullname,
    } <= set(Base.metadata.tables)
    assert "ix_ops_scheduler_heartbeat_scheduler_name" == next(
        index.name for index in SchedulerHeartbeat.__table__.indexes
    )
    assert "uq_operational_alert_alert_key" in {
        constraint.name for constraint in OperationalAlert.__table__.constraints
    }
    assert {"stale_reason", "freshness_checked_at"} <= set(
        PublishedSnapshot.__table__.c.keys()
    )

    engine = pg_factory.kw["bind"]

    names = set(inspect(engine).get_table_names(schema="ops"))

    assert {
        "scheduler_heartbeat",
        "scheduler_checkpoint",
        "operational_alert",
    } <= names


@pytest.mark.parametrize("constraint_exists", [False, True])
def test_lineage_migration_only_drops_existing_legacy_constraint(
    monkeypatch: pytest.MonkeyPatch,
    constraint_exists: bool,
) -> None:
    migration = import_module(
        "migrations.versions.20260820_0004_preserve_source_payload_lineage"
    )
    constraints = [{"name": "uq_source_payload_content"}] if constraint_exists else []
    dropped: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeInspector:
        def get_unique_constraints(
            self,
            table_name: str,
            *,
            schema: str,
        ) -> list[dict[str, str]]:
            assert table_name == "source_payload"
            assert schema == "raw"
            return constraints

    monkeypatch.setattr(migration, "inspect", lambda bind: FakeInspector(), raising=False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )

    migration.upgrade()

    assert bool(dropped) is constraint_exists


def test_lineage_migration_downgrade_does_not_destroy_valid_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module(
        "migrations.versions.20260820_0004_preserve_source_payload_lineage"
    )
    created: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        migration.op,
        "create_unique_constraint",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    migration.downgrade()

    assert created == []


@pytest.mark.parametrize(
    ("existing", "expected_added"),
    [
        (set(), {"request_payload", "last_attempted_at", "resolved_at"}),
        (
            {"request_payload", "last_attempted_at", "resolved_at"},
            set(),
        ),
        (
            {"request_payload"},
            {"last_attempted_at", "resolved_at"},
        ),
    ],
)
def test_retry_migration_adds_only_missing_columns(
    monkeypatch: pytest.MonkeyPatch,
    existing: set[str],
    expected_added: set[str],
) -> None:
    migration = import_module(
        "migrations.versions.20260824_0005_retry_failed_requests"
    )
    added: list[str] = []

    class FakeInspector:
        def get_columns(
            self,
            table_name: str,
            *,
            schema: str,
        ) -> list[dict[str, str]]:
            assert table_name == "job_failure"
            assert schema == "ops"
            return [{"name": name} for name in existing]

    monkeypatch.setattr(migration, "inspect", lambda bind: FakeInspector(), raising=False)
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda _table, column, **_kwargs: added.append(column.name),
    )

    migration.upgrade()

    assert set(added) == expected_added
