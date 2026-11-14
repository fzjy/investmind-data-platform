"""独立审查的四组反例；不使用业务库或真实工具。"""
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.ops.backup import files, inventory, service, tools
from app.ops.backup.config import backup_options, parse_target
from app.ops.backup.contracts import BackupError
from backend.tests.ops.backup.test_files import backup_id
from backend.tests.ops.backup.test_service import ControlledBackup, settings
from backend.tests.ops.backup.test_tools import ControlledProcesses
from backend.tests.ops.backup.test_verification import ControlledRestore
from backend.tests.ops.backup.test_files import publish


def test_posix_last_check_race_never_unlinks_outside_directory(tmp_path, monkeypatch):
    # 模拟POSIX系统调用边界，不在Windows假称实际macOS运行。
    swapped = False
    outside_deleted = False
    next_fd = 10
    directory = SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=1, st_ino=100, st_nlink=1)
    changed = SimpleNamespace(st_mode=stat.S_IFLNK, st_dev=1, st_ino=200, st_nlink=1)
    regular = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=300, st_nlink=1)
    identifier = backup_id()
    def opened(name, flags, *, dir_fd=None):
        nonlocal next_fd
        assert flags & 0x20000 and flags & 0x10000
        if name == identifier:
            return 100
        next_fd += 1
        return next_fd
    def metadata(name, *, dir_fd=None, follow_symlinks=True):
        if name == identifier:
            return changed if swapped else directory
        return regular
    def unlink(name, *, dir_fd=None):
        nonlocal swapped, outside_deleted
        swapped = True  # 最后一次检查后、真正unlink前替换父目录。
        if dir_fd != 100:
            outside_deleted = True
    proxy = SimpleNamespace(**vars(os))
    proxy.name = "posix"
    proxy.O_DIRECTORY, proxy.O_NOFOLLOW = 0x10000, 0x20000
    proxy.open, proxy.stat, proxy.fstat = opened, metadata, lambda fd: directory
    proxy.unlink, proxy.rmdir, proxy.close = unlink, lambda *a, **k: None, lambda fd: None
    proxy.listdir = lambda fd: ["database.dump", "manifest.json"]
    proxy.fsync = lambda fd: None
    monkeypatch.setattr(files, "os", proxy)
    monkeypatch.setattr(files, "_guard", lambda path: nullcontext())
    monkeypatch.setattr(files, "_root_path", lambda path: path)
    monkeypatch.setattr(files, "_check_path", lambda path: None)
    monkeypatch.setattr(files, "_sync_directory", lambda path: None)
    monkeypatch.setattr(Path, "unlink", lambda path, **kwargs: unlink(str(path)))
    monkeypatch.setattr(Path, "rmdir", lambda path: None)
    store = files.BackupStore(tmp_path / "packages")
    monkeypatch.setattr(store, "load", lambda value: None)
    monkeypatch.setattr(store, "_members", lambda path: frozenset({"database.dump", "manifest.json"}))
    try:
        store.delete_package(identifier)
    except BackupError:
        pass
    assert not outside_deleted


def test_dropped_column_slots_do_not_change_logical_schema_fingerprint():
    class Rows:
        def __init__(self, values): self.values = values
        def mappings(self): return iter(self.values)
    class Reader:
        def __init__(self, numbers): self.numbers = numbers
        def execute(self, connection, sql, parameters=None):
            if "pg_attribute" not in sql:
                return Rows([])
            return Rows([{"attnum": number, "attname": name, "type": "integer"} for number, name in zip(self.numbers, ("a", "c"), strict=True)])
    # 即使数据库查询适配器给出物理槽位，也必须在清单边界规范化。
    assert inventory._structure(None, {"oid": 1}, Reader((1, 3))) == inventory._structure(None, {"oid": 2}, Reader((1, 2)))


def test_read_inventory_stops_stream_at_deadline(monkeypatch):
    clock = [0.0]
    consumed = []
    closed = []
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    def rows():
        for number in range(5):
            consumed.append(number)
            clock[0] += 0.4
            yield '{"v":1}'
    class Result:
        def __init__(self, values=()): self.values = values
        def scalars(self): return iter(self.values)
        def close(self): closed.append(True)
    class Connection:
        def execute(self, sql, params=None):
            return Result(rows()) if "to_jsonb(t)::text AS row_data" in str(sql) else Result()
    monkeypatch.setattr(inventory, "_catalog", lambda connection, budget: inventory.CatalogView(({"kind": "r", "schema_name": "core", "name": "security", "oid": 1},), {}))
    monkeypatch.setattr(inventory, "_structure", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr(inventory, "_sequences", lambda *args: ())
    with pytest.raises(BackupError):
        inventory.read_inventory(Connection(), timeout=1)
    assert consumed == [0, 1, 2]
    assert closed == [True]


def test_docker_preparation_reduces_tool_timeout(tmp_path, monkeypatch):
    clock = [0.0]
    observed = []
    fake = ControlledProcesses()
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    def run(argv, *, timeout, **kwargs):
        if argv[1] == "start": clock[0] += 9
        if "--format=custom" in argv: observed.append(timeout)
        return fake(argv, timeout=timeout, **kwargs)
    monkeypatch.setattr(tools, "run_process", run)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    tools.DockerTools(backup_options(Settings(_env_file=None))).dump(parse_target("postgresql://u:p@localhost/db"), "1-2-3", tmp_path / "database.dump", 10)
    assert len(observed) == 1 and 0 < observed[0] <= 1


def test_expired_preparation_still_cleans_owned_helper(tmp_path, monkeypatch):
    clock = [0.0]
    fake = ControlledProcesses()
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    def run(argv, *, timeout, **kwargs):
        if argv[1] == "start": clock[0] += 11
        assert timeout > 0
        return fake(argv, timeout=timeout, **kwargs)
    monkeypatch.setattr(tools, "run_process", run)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError):
        tools.DockerTools(backup_options(Settings(_env_file=None))).dump(parse_target("postgresql://u:p@localhost/db"), "1-2-3", tmp_path / "database.dump", 10)
    assert any(command[1] == "rm" for command in fake.commands)
    assert not any("--format=custom" in command for command in fake.commands)


def test_native_credential_preparation_reduces_tool_timeout(tmp_path, monkeypatch):
    clock = [0.0]
    observed = []
    fake = ControlledProcesses()
    original = tools.temporary_passfile
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    @contextmanager
    def slow_credentials(target):
        with original(target) as path:
            clock[0] += 9
            yield path
    def run(argv, *, timeout, **kwargs):
        observed.append(timeout)
        return fake(argv, timeout=timeout, **kwargs)
    monkeypatch.setattr(tools, "temporary_passfile", slow_credentials)
    monkeypatch.setattr(tools, "run_process", run)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    tools.NativeTools(backup_options(Settings(_env_file=None, backup_tool_mode="native"))).dump(parse_target("postgresql://u:p@localhost/db"), "1-2-3", tmp_path / "database.dump", 10)
    assert observed == [1]


@pytest.mark.parametrize("late_boundary", ["manifest", "rename"])
def test_publish_timeout_never_leaves_complete_package(tmp_path, monkeypatch, late_boundary):
    clock = [0.0]
    fake = ControlledBackup()
    fake.install(service, monkeypatch)
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    sync = files._sync_directory
    def late_sync(path):
        sync(path)
        if ((late_boundary == "manifest" and path.name.startswith(".partial-"))
                or (late_boundary == "rename" and path.name == "packages")):
            clock[0] += 61
    monkeypatch.setattr(files, "_sync_directory", late_sync)
    root = tmp_path / "packages"
    with pytest.raises(BackupError):
        service.create_backup(settings(root).model_copy(update={"backup_timeout_seconds": 60}), force=True)
    assert files.BackupStore(root).scan() == ()


def test_late_verification_write_preserves_previous_report(tmp_path, monkeypatch):
    root = tmp_path / "packages"
    identifier, path = publish(files.BackupStore(root))
    fake = ControlledRestore()
    fake.install(service, monkeypatch)
    service.verify_backup(settings(root), backup_id=identifier)
    original = (path / "verification.json").read_bytes()
    clock = [0.0]
    calls = [0]
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    sync = files._sync_directory
    def late_sync(directory):
        sync(directory)
        calls[0] += 1
        if calls[0] == 2: clock[0] += 61  # 原子 replace 后的目录同步。
    monkeypatch.setattr(files, "_sync_directory", late_sync)
    with pytest.raises(BackupError):
        service.verify_backup(settings(root).model_copy(update={"backup_timeout_seconds": 60}), backup_id=identifier)
    assert fake.cleaned
    assert (path / "verification.json").read_bytes() == original
