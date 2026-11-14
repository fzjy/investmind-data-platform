from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import time
import uuid

from sqlalchemy import create_engine, Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from app.config import PROJECT_ROOT, Settings
from app.ops.backup.config import backup_options, parse_target, source_identity
from app.ops.backup.contracts import BackupError, BackupManifest, BackupResult, ConnectionTarget
from app.ops.backup.files import BackupStore, backup_lock, file_sha256
from app.ops.backup.inventory import Budget, assert_restored_inventory, check_sequences, read_inventory, snapshot_inventory
from app.ops.backup.api_probe import probe_restored_api
from app.ops.backup.process import run_process, runtime_environment
from app.ops.backup.tools import ensure_compatible_versions, open_tools
from app.ops.backup.targets import open_restore_target
from app.ops.backup.retention import plan_retention
from app.ops.backup.budget import settings_budget


def open_source_engine(target: ConnectionTarget) -> Engine:
    url = URL.create("postgresql+psycopg", username=target.username, password=target.password.get_secret_value(), host=target.host, port=target.port, database=target.database)
    return create_engine(url, connect_args={"sslmode": target.sslmode, "connect_timeout": target.connect_timeout,
        "application_name": "smartparse-backup", "options": "-c default_transaction_read_only=on -c timezone=UTC"})


def current_code_head() -> str:
    executable = shutil.which("git")
    if executable is None:
        return "unavailable"
    try:
        return run_process([executable, "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], env=runtime_environment(), timeout=10).decode("ascii").strip()
    except (BackupError, UnicodeError):
        return "unavailable"


def _latest(store: BackupStore, identity: str) -> BackupManifest | None:
    packages = [package for package in store.scan() if package.source_identity == identity]
    return packages[-1] if packages else None


def _is_due(latest: BackupManifest | None, now: datetime, days: int) -> bool:
    return latest is None or now >= latest.completed_at + timedelta(days=days)


def _result(status: str, reason: str, options, started: datetime, elapsed: float, latest: BackupManifest | None = None) -> BackupResult:
    return BackupResult(status=status, stage="backup", reason_code=reason, tool_mode=options.tool_mode, started_at=started,
        ended_at=datetime.now(timezone.utc), elapsed_seconds=max(0, time.monotonic() - elapsed),
        backup_id=latest.backup_id if latest else None, archive_size=latest.archive_size if latest else 0,
        table_count=len(latest.inventory.tables) if latest else 0, verification_level="archive_valid" if latest else "none",
        archive_sha256=latest.archive_sha256 if latest else None)


@settings_budget
def create_backup(settings: Settings, *, force: bool = False, now: datetime | None = None) -> BackupResult:
    options = backup_options(settings)
    target = parse_target(settings.database_url)
    store = BackupStore(options.path)
    started = now or datetime.now(timezone.utc)
    if started.utcoffset() is None:
        raise BackupError("invalid_backup_time", 2, "configuration")
    identity = source_identity(target)
    elapsed = time.monotonic()
    budget = Budget(options.timeout_seconds)
    latest = _latest(store, identity)
    if not force and not _is_due(latest, started, options.interval_days):
        return _result("skipped", "backup_not_due", options, started, elapsed, latest)
    with backup_lock(store.root):
        latest = _latest(store, identity)
        if not force and not _is_due(latest, started, options.interval_days):
            return _result("skipped", "backup_not_due", options, started, elapsed, latest)
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
        stage = store.stage(identifier)
        source = None
        try:
            target_for_connection = target.model_copy(update={"connect_timeout": min(target.connect_timeout, max(1, int(budget.remaining())))})
            source = open_source_engine(target_for_connection)
            with open_tools(options) as tools:
                versions = tools.versions()
                with snapshot_inventory(source, timeout=budget.remaining()) as view:
                    ensure_compatible_versions(versions, view.server_major)
                    inventory = read_inventory(view.connection, timeout=budget.remaining())
                    archive = stage / "database.dump"
                    tools.dump(target, view.snapshot_id, archive, budget.remaining())
                    tools.list_archive(archive, budget.remaining())
                    archive_hash = file_sha256(archive)
                    budget.remaining()
                    manifest = BackupManifest(backup_id=identifier, source_identity=identity, completed_at=datetime.now(timezone.utc),
                        archive_sha256=archive_hash, archive_size=archive.stat().st_size, tool_mode=options.tool_mode,
                        dump_version=versions[0], restore_version=versions[1], server_major=view.server_major,
                        code_head=current_code_head(), inventory=inventory)
            budget.remaining()
            store.promote(identifier, manifest)
            return _result("completed", "archive_valid", options, started, elapsed, manifest)
        except (SQLAlchemyError, OSError):
            raise BackupError("backup_operation_failed", 3, "backup") from None
        finally:
            if source is not None:
                source.dispose()


@settings_budget
def verify_backup(settings: Settings, *, backup_id: str | None = None) -> BackupResult:
    options = backup_options(settings)
    store = BackupStore(options.path)
    started = datetime.now(timezone.utc)
    elapsed = time.monotonic()
    budget = Budget(options.timeout_seconds)
    with backup_lock(store.root):
        if backup_id is None:
            latest = _latest(store, source_identity(parse_target(settings.database_url)))
            if latest is None:
                raise BackupError("no_complete_backup", 2, "verification")
            backup_id = latest.backup_id
        manifest = store.load(backup_id)
        archive = store.root / backup_id / "database.dump"
        manifest_hash = file_sha256(store.root / backup_id / "manifest.json")
        with open_tools(options) as tools:
            ensure_compatible_versions(tools.versions(), manifest.server_major)
            tools.list_archive(archive, budget.remaining())
            with open_restore_target(options) as restored:
                restored.assert_owned()
                target_engine = open_source_engine(restored.target)
                try:
                    with snapshot_inventory(target_engine, timeout=budget.remaining()) as view:
                        if view.server_major != manifest.server_major:
                            raise BackupError("postgres_major_mismatch", 2, "preflight")
                    restored.assert_owned()
                    tools.restore(restored.target, archive, budget.remaining())
                    try:
                        with snapshot_inventory(target_engine, timeout=budget.remaining()) as view:
                            recovered = read_inventory(view.connection, timeout=budget.remaining())
                            assert_restored_inventory(view.connection, recovered, manifest.inventory, timeout=budget.remaining())
                            check_sequences(view.connection, recovered)
                    except BackupError as error:
                        if error.reason_code == "unsupported_database_objects":
                            raise BackupError("restored_inventory_mismatch", 4, "verification") from None
                        raise
                    probe_restored_api(restored.target, manifest.inventory, timeout=budget.remaining())
                    budget.remaining()
                except (SQLAlchemyError, OSError):
                    raise BackupError("restore_operation_failed", 3, "verification") from None
                finally:
                    target_engine.dispose()
        # 自有目标清理完成才记成功；归档或 manifest 在核对期间变化也不能记验证。
        store.load(backup_id)
        if file_sha256(store.root / backup_id / "manifest.json") != manifest_hash:
            raise BackupError("manifest_changed_during_verification", 4, "verification")
        report = BackupResult(status="completed", stage="verification", reason_code="restore_verified",
            backup_id=backup_id, tool_mode=options.tool_mode, started_at=started, ended_at=datetime.now(timezone.utc),
            elapsed_seconds=time.monotonic() - elapsed, archive_size=manifest.archive_size, table_count=len(manifest.inventory.tables),
            verification_level="restore_verified", archive_sha256=manifest.archive_sha256, manifest_sha256=manifest_hash)
        store.write_verification(backup_id, report)
        return report


def _local_state(store: BackupStore):
    packages = store.scan()
    verified: set[str] = set()
    signatures: dict[str, tuple[str, str | None]] = {}
    anomalies = store.anomaly_count
    for package in packages:
        path = store.root / package.backup_id
        report_path = path / "verification.json"
        report = store.read_verification(package.backup_id)
        if report is not None:
            verified.add(package.backup_id)
        elif report_path.exists():
            anomalies += 1
        signatures[package.backup_id] = (file_sha256(path / "manifest.json"), file_sha256(report_path) if report_path.exists() else None)
    return packages, frozenset(verified), signatures, anomalies


def read_backup_status(settings: Settings, *, now: datetime | None = None) -> BackupResult:
    options = backup_options(settings)
    identity = source_identity(parse_target(settings.database_url))
    store = BackupStore(options.path)
    started = now or datetime.now(timezone.utc)
    elapsed = time.monotonic()
    packages, verified, _, anomalies = _local_state(store)
    same_source = tuple(package for package in packages if package.source_identity == identity)
    latest = same_source[-1] if same_source else None
    last_verified = next((package for package in reversed(same_source) if package.backup_id in verified), None)
    candidates = plan_retention(same_source, verified_ids=verified, now=started, keep_count=options.keep_count, retention_days=options.retention_days)
    due = _is_due(latest, started, options.interval_days)
    return BackupResult(status="ready" if latest else "empty", stage="status", reason_code="backup_due" if due else "backup_not_due",
        backup_id=latest.backup_id if latest else None, tool_mode=options.tool_mode,
        started_at=started, ended_at=datetime.now(timezone.utc), elapsed_seconds=time.monotonic() - elapsed,
        archive_size=latest.archive_size if latest else 0, table_count=len(latest.inventory.tables) if latest else 0,
        verification_level="restore_verified" if latest and latest.backup_id in verified else "archive_valid" if latest else "none",
        latest_verified_id=last_verified.backup_id if last_verified else None, due=due, candidates=candidates,
        anomaly_count=anomalies, candidate_count=len(candidates),
        verification_expired=_is_due(last_verified, started, options.interval_days))


def prune_backups(settings: Settings, *, apply: bool = False, now: datetime | None = None) -> BackupResult:
    options = backup_options(settings)
    identity = source_identity(parse_target(settings.database_url))
    store = BackupStore(options.path)
    started = now or datetime.now(timezone.utc)
    elapsed = time.monotonic()
    packages, verified, signatures, anomalies = _local_state(store)
    same_source = tuple(package for package in packages if package.source_identity == identity)
    candidates = plan_retention(same_source, verified_ids=verified, now=started, keep_count=options.keep_count, retention_days=options.retention_days)
    if apply:
        with backup_lock(store.root):
            locked_packages, locked_verified, locked_signatures, _ = _local_state(store)
            locked_source = tuple(package for package in locked_packages if package.source_identity == identity)
            locked_candidates = plan_retention(locked_source, verified_ids=locked_verified, now=started, keep_count=options.keep_count, retention_days=options.retention_days)
            if locked_signatures != signatures or locked_candidates != candidates:
                raise BackupError("backup_state_changed_before_prune", 2, "prune")
            for identifier in candidates:
                path = store.root / identifier
                store.load(identifier)
                verification = path / "verification.json"
                current = (file_sha256(path / "manifest.json"), file_sha256(verification) if verification.exists() else None)
                if current != signatures[identifier]:
                    raise BackupError("backup_state_changed_before_prune", 2, "prune")
                try:
                    store.delete_package(identifier)
                except OSError:
                    raise BackupError("backup_prune_failed", 3, "prune") from None
    return BackupResult(status="pruned" if apply else "planned", stage="prune", reason_code="backup_prune_applied" if apply else "backup_prune_dry_run",
        tool_mode=options.tool_mode, started_at=started, ended_at=datetime.now(timezone.utc), elapsed_seconds=time.monotonic() - elapsed,
        candidates=candidates, candidate_count=len(candidates), anomaly_count=anomalies)
