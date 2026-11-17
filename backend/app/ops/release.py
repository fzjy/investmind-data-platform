"""只读发布计划与备份驱动的一次性副本升级演练。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Iterator, Literal

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from pydantic import Field
from sqlalchemy import Connection, Engine, inspect

from app.config import PROJECT_ROOT, Settings
from app.db.base import ALL_BASES
from app.db import models  # noqa: F401
from app.ops.backup import service
from app.ops.backup.budget import deadline_scope
from app.ops.backup.config import backup_options
from app.ops.backup.contracts import BackupError, BackupManifest, Contract, DatabaseInventory
from app.ops.backup.files import BackupStore, backup_lock, file_sha256
from app.ops.backup.inventory import Budget, USER_NAMESPACE, fingerprint_rows, quote_identifier
from app.ops.backup.targets import RestoreTarget


class AuditStep(Contract):
    stage: str
    status: Literal["completed", "failed"]
    reason_code: str


class ReleasePlan(Contract):
    status: Literal["planned"] = "planned"
    source_versions: tuple[str, ...] = ()
    target_versions: tuple[str, ...] = ()
    migrations: tuple[str, ...] = ()
    version_evidence: Literal["not_read", "backup_manifest"] = "not_read"
    production_executed: Literal[False] = False
    recovery_strategy: Literal["backup_restore"] = "backup_restore"
    steps: tuple[str, ...] = ("stop_jobs", "backup", "verify_copy", "upgrade", "read_acceptance", "resume_jobs")
    recovery_steps: tuple[str, ...] = ("stop_jobs", "restore_backup_to_new_database", "verify_original_fingerprints", "switch_database_and_previous_code", "read_acceptance", "resume_jobs")
    recovery_note: str = "恢复需切换已验收的备份副本和原代码版本；生产切换、原代码运行及角色权限须单独验收。"


class ReleaseReport(Contract):
    status: Literal["completed", "blocked", "failed", "busy"]
    reason_code: str
    exit_code: int = 0
    source_versions: tuple[str, ...] = ()
    target_versions: tuple[str, ...] = ()
    actual_versions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    audit: tuple[AuditStep, ...] = ()
    api: dict[str, object] = Field(default_factory=dict)
    recovery_verified: bool = False
    recovery_strategy: Literal["backup_restore"] = "backup_restore"
    production_executed: Literal[False] = False
    compatibility_scope: str = "existing_columns_rows_and_published_api"
    backup_id: str | None = None
    archive_sha256: str | None = None
    manifest_sha256: str | None = None
    started_at: datetime
    ended_at: datetime
    elapsed_seconds: float = 0
    plan: ReleasePlan | None = None


def _scripts() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "backend/migrations"))
    script = ScriptDirectory.from_config(config)
    if len(script.get_heads()) != 1:
        raise BackupError("ambiguous_release_heads", 2, "preflight")
    return script


def _load_package(package: str | Path) -> tuple[BackupStore, BackupManifest]:
    value = str(package)
    if not value or "://" in value or any(ord(char) < 32 for char in value):
        raise BackupError("release_package_required", 2, "preflight")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    # 保留未解析路径，让 BackupStore 逐级检查 symlink/junction。
    if not path.is_dir():
        raise BackupError("release_package_required", 2, "preflight")
    store = BackupStore(path.parent)
    return store, store.load(path.name)


def _plan(manifest: BackupManifest | None) -> ReleasePlan:
    script = _scripts()
    source = manifest.inventory.alembic_versions if manifest else ()
    migrations: tuple[str, ...] = ()
    if manifest is not None:
        if len(source) != 1:
            raise BackupError("ambiguous_release_source_version", 2, "preflight")
        # 只接受清单中的真实 revision，不允许 head/base 等命令符号。
        revisions = {item.revision for item in script.walk_revisions()}
        if source[0] not in revisions:
            raise BackupError("unknown_release_source_version", 2, "preflight")
        migrations = tuple(item.revision for item in reversed(list(script.iterate_revisions(script.get_current_head(), source[0]))))
    return ReleasePlan(source_versions=source, target_versions=tuple(script.get_heads()), migrations=migrations,
        version_evidence="backup_manifest" if manifest else "not_read")


def release_plan(*, package: str | Path | None = None) -> ReleasePlan:
    """不读取 .env、不连接数据库、不创建锁或写入备份包。"""
    return _plan(_load_package(package)[1] if package is not None else None)


def read_alembic_versions(connection: Connection, *, timeout: float) -> dict[str, tuple[str, ...]]:
    """逐 schema 读取实际版本；只读报告不等于授权迁移其他应用。"""
    budget = Budget(timeout)
    schemas = list(budget.execute(connection, f"""SELECT n.nspname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {USER_NAMESPACE} AND c.relkind='r' AND c.relname='alembic_version'
        ORDER BY n.nspname COLLATE "C"
        """
    ).scalars())
    return {schema: tuple(budget.execute(connection,
        f"SELECT version_num FROM {quote_identifier(schema)}.alembic_version ORDER BY version_num").scalars()) for schema in schemas}


def _upgrade_copy(restored: RestoreTarget, *, timeout: float) -> None:
    """唯一写迁移路径必须持有既有恢复层的自有目标证明。"""
    restored.assert_owned()
    script = _scripts()
    budget = Budget(timeout)
    engine = service.open_source_engine(restored.target)
    try:
        with engine.connect() as connection:
            versions = read_alembic_versions(connection, timeout=budget.remaining())
            if set(versions) != {"public"} or len(versions["public"]) != 1:
                raise BackupError("unsupported_release_version_layout", 2, "upgrade")
            current = versions["public"][0]
            if current not in {item.revision for item in script.walk_revisions()}:
                raise BackupError("unknown_release_source_version", 2, "upgrade")
            connection.rollback()
            restored.assert_owned()
            connection.exec_driver_sql("SET default_transaction_read_only=off")
            connection.commit()
            with connection.begin():
                budget.execute(connection, "SELECT set_config('lock_timeout', :timeout, true)", {"timeout": str(min(10000, max(1, int(budget.remaining() * 1000))))})
                # 沿用 Alembic upgrade 的迁移步骤及版本维护，绑定自有连接；
                # 不执行会优先读取业务 settings.database_url 的 env.py。
                config = Config()
                config.set_main_option("script_location", script.dir)
                with EnvironmentContext(config, script, fn=lambda revision, context: script._upgrade_revs("head", revision)) as environment:
                    environment.configure(connection=connection, version_table_schema="public")
                    environment.run_migrations()
                budget.remaining()
    finally:
        engine.dispose()


def _legacy_columns(connection: Connection, inventory: DatabaseInventory) -> dict[tuple[str, str], tuple[str, ...]]:
    inspector = inspect(connection)
    return {(table.schema_name, table.table_name): tuple(column["name"] for column in inspector.get_columns(table.table_name, schema=table.schema_name))
        for table in inventory.tables if table.table_name != "alembic_version"}


def _check_code_schema(connection: Connection, *, timeout: float) -> None:
    """不把 stamp/head 当作 schema 验收；核对当前代码所需列、类型和 nullable。"""
    budget = Budget(timeout)
    inspector = inspect(connection)
    for base in ALL_BASES:
        for table in base.metadata.sorted_tables:
            budget.execute(connection, "SELECT 1")
            if not inspector.has_table(table.name, schema=table.schema):
                raise BackupError("release_schema_mismatch", 4, "schema_acceptance")
            actual = {column["name"]: column for column in inspector.get_columns(table.name, schema=table.schema)}
            for column in table.columns:
                found = actual.get(column.name)
                if (found is None or found["nullable"] != column.nullable
                        or found["type"].compile(dialect=connection.dialect) != column.type.compile(dialect=connection.dialect)):
                    raise BackupError("release_schema_mismatch", 4, "schema_acceptance")
            budget.remaining()


# 与 0021 的 UPDATE 保持相同的产品范围、历史版本、时刻及排除字段。
_HISTORY_MATCH_0021 = """h.product=p.product AND h.snapshot_version=p.version AND h.as_of=p.as_of
    AND p.product IN ('macro-insights','macro-transmission','market-overview','portfolio','screener')
    AND NOT jsonb_exists_any(h.payload::jsonb, ARRAY['marketNews','dailyChart'])"""
_REFERENCE_0021 = "to_json('@investmind-history:' || md5(h.payload::text))::text"
_EXPECTED_POINTERS_0021 = f"""SELECT p.product,p.version,{_REFERENCE_0021}
    FROM mart.published_snapshot p JOIN mart.page_snapshot h ON {_HISTORY_MATCH_0021}
    WHERE p.payload::text=h.payload::text"""
_ACTUAL_POINTERS_0021 = """SELECT product,version,payload::text FROM mart.published_snapshot
    WHERE json_typeof(payload)='string'"""

# 指针与实际历史正文不符时，NULL 与原始正文指纹不同。
_HISTORY_PAYLOAD_0021 = f"""CASE WHEN json_typeof(p.payload) <> 'string' THEN p.payload ELSE (
    SELECT h.payload FROM mart.page_snapshot h WHERE {_HISTORY_MATCH_0021}
      AND p.payload::text = {_REFERENCE_0021}
) END AS payload"""


def _legacy_fingerprints(connection: Connection, columns: dict[tuple[str, str], tuple[str, ...]], *,
                         timeout: float, crossed_0021: bool = False) -> dict[tuple[str, str], tuple[int, str]]:
    budget = Budget(timeout)
    result = {}
    for (schema, table), names in columns.items():
        qualified = quote_identifier(schema) + "." + quote_identifier(table)
        resolve_payload = crossed_0021 and (schema, table) == ("mart", "published_snapshot")
        projection = ", ".join(
            _HISTORY_PAYLOAD_0021 if resolve_payload and name == "payload" else quote_identifier(name)
            for name in names
        )
        rows = budget.execute(connection, f'SELECT to_jsonb(t)::text FROM (SELECT {projection} FROM {qualified} AS p) t ORDER BY to_jsonb(t)::text COLLATE "C"', stream=True)
        try:
            result[(schema, table)] = fingerprint_rows(rows.scalars(), check=budget.remaining)
        finally:
            rows.close()
    return result


@contextmanager
def _verified_copy_snapshot(engine: Engine, *, timeout: float, stage: str):
    """restore/upgrade后的自有副本不支持对象属于验收失败，不是源配置阻断。"""
    try:
        with service.snapshot_inventory(engine, timeout=timeout) as view:
            yield view
    except BackupError as error:
        if error.reason_code == "unsupported_database_objects":
            raise BackupError("restored_inventory_mismatch", 4, stage) from None
        raise


@contextmanager
def _original_copy(options, tools, archive: Path, manifest: BackupManifest, budget: Budget) -> Iterator[tuple[RestoreTarget, Engine]]:
    with service.open_restore_target(options) as restored:
        restored.assert_owned()
        engine = service.open_source_engine(restored.target)
        try:
            with service.snapshot_inventory(engine, timeout=budget.remaining()) as view:
                if view.server_major != manifest.server_major:
                    raise BackupError("postgres_major_mismatch", 2, "preflight")
            restored.assert_owned()
            tools.restore(restored.target, archive, budget.remaining())
            with _verified_copy_snapshot(engine, timeout=budget.remaining(), stage="restore_fingerprints") as view:
                service.assert_restored_inventory(view.connection, service.read_inventory(view.connection, timeout=budget.remaining()), manifest.inventory, timeout=budget.remaining())
                service.check_sequences(view.connection, manifest.inventory)
            yield restored, engine
        finally:
            engine.dispose()


def verify_release(settings: Settings, *, package: str | Path) -> ReleaseReport:
    """不接受目标 URL；成功包括真实升级及第二个副本的备份恢复演练。"""
    started = datetime.now(timezone.utc)
    elapsed = time.monotonic()
    audit: list[AuditStep] = []
    plan = ReleasePlan()
    manifest = None
    manifest_hash = None
    actual: dict[str, tuple[str, ...]] = {}
    api: dict[str, object] = {}
    recovery_verified = False
    stage = "preflight"
    failure: BackupError | None = None

    def done(name: str) -> None:
        audit.append(AuditStep(stage=name, status="completed", reason_code="verified"))

    def failed(error: Exception, name: str) -> BackupError:
        safe = error if isinstance(error, BackupError) else BackupError("release_operation_failed", 3, name)
        audit.append(AuditStep(stage=name, status="failed", reason_code=safe.reason_code))
        return safe

    try:
        options = backup_options(settings)
        with deadline_scope(options.timeout_seconds):
            budget = Budget(options.timeout_seconds)
            store, manifest = _load_package(package)
            plan = _plan(manifest)
            done(stage)
            with backup_lock(store.root):
                locked = store.load(manifest.backup_id)
                if locked != manifest:
                    raise BackupError("manifest_changed_during_verification", 4, "package_binding")
                archive = store.root / manifest.backup_id / "database.dump"
                manifest_hash = file_sha256(store.root / manifest.backup_id / "manifest.json")
                with service.open_tools(options) as tools:
                    service.ensure_compatible_versions(tools.versions(), manifest.server_major)
                    tools.list_archive(archive, budget.remaining())
                    try:
                        stage = "restore_fingerprints"
                        with _original_copy(options, tools, archive, manifest, budget) as (restored, engine):
                            done(stage)
                            stage = "version_preflight"
                            with service.snapshot_inventory(engine, timeout=budget.remaining()) as view:
                                actual = read_alembic_versions(view.connection, timeout=budget.remaining())
                                if actual != {"public": plan.source_versions}:
                                    raise BackupError("unsupported_release_version_layout", 2, stage)
                                columns = _legacy_columns(view.connection, manifest.inventory)
                                check_history = ("20261115_0021" in plan.migrations
                                                 and ("mart", "published_snapshot") in columns)
                                expected_pointers = set()
                                if check_history and ("mart", "page_snapshot") in columns:
                                    expected_pointers = set(budget.execute(view.connection, _EXPECTED_POINTERS_0021))
                                baseline = _legacy_fingerprints(view.connection, columns, timeout=budget.remaining())
                            done(stage)
                            stage = "upgrade"
                            _upgrade_copy(restored, timeout=budget.remaining())
                            done(stage)
                            stage = "version_acceptance"
                            with _verified_copy_snapshot(engine, timeout=budget.remaining(), stage=stage) as view:
                                actual = read_alembic_versions(view.connection, timeout=budget.remaining())
                                if actual != {"public": plan.target_versions}:
                                    raise BackupError("release_version_mismatch", 4, stage)
                                done(stage)
                                stage = "schema_acceptance"
                                _check_code_schema(view.connection, timeout=budget.remaining())
                                done(stage)
                                stage = "legacy_data"
                                upgraded = _legacy_fingerprints(view.connection, columns, timeout=budget.remaining(),
                                    crossed_0021=check_history)
                                if check_history and set(budget.execute(view.connection, _ACTUAL_POINTERS_0021)) != expected_pointers:
                                    raise BackupError("release_legacy_data_mismatch", 4, stage)
                                if upgraded != baseline:
                                    raise BackupError("release_legacy_data_mismatch", 4, stage)
                                inventory = service.read_inventory(view.connection, timeout=budget.remaining())
                                if inventory.publications != manifest.inventory.publications:
                                    raise BackupError("release_publications_mismatch", 4, stage)
                                service.check_sequences(view.connection, inventory)
                                done(stage)
                            stage = "api_acceptance"
                            api = service.probe_restored_api(restored.target, manifest.inventory, timeout=budget.remaining())
                            done(stage)
                            stage = "cleanup"
                        stage = "cleanup"
                        done(stage)
                    except Exception as error:
                        failure = failed(error, stage)
                    # 升级失败仍独立演练原始备份恢复；不对已升级副本 downgrade。
                    stage = "backup_restore"
                    try:
                        with _original_copy(options, tools, archive, manifest, budget) as (restored, engine):
                            with service.snapshot_inventory(engine, timeout=budget.remaining()) as view:
                                if read_alembic_versions(view.connection, timeout=budget.remaining()) != {"public": plan.source_versions}:
                                    raise BackupError("release_recovery_version_mismatch", 4, stage)
                            service.probe_restored_api(restored.target, manifest.inventory, timeout=budget.remaining())
                        recovery_verified = True
                        done(stage)
                    except Exception as error:
                        recovery_error = failed(error, stage)
                        if failure is None:
                            failure = recovery_error
                stage = "package_binding"
                store.load(manifest.backup_id)
                if file_sha256(store.root / manifest.backup_id / "manifest.json") != manifest_hash:
                    raise BackupError("manifest_changed_during_verification", 4, stage)
                done(stage)
                budget.remaining()
    except Exception as error:
        outer_error = failed(error, stage)
        if failure is None:
            failure = outer_error
    code = failure.exit_code if failure else 0
    return ReleaseReport(status="busy" if code == 5 else "blocked" if code == 2 else "failed" if code else "completed",
        reason_code=failure.reason_code if failure else "release_copy_verified", exit_code=code,
        source_versions=plan.source_versions if plan else (), target_versions=plan.target_versions if plan else (),
        actual_versions=actual, audit=tuple(audit), api=api, recovery_verified=recovery_verified,
        backup_id=manifest.backup_id if manifest else None, archive_sha256=manifest.archive_sha256 if manifest else None,
        manifest_sha256=manifest_hash, started_at=started, ended_at=datetime.now(timezone.utc),
        elapsed_seconds=time.monotonic() - elapsed, plan=plan)
