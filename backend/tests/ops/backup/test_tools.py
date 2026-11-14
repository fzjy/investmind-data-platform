from __future__ import annotations

import importlib
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.config import Settings
from app.ops.backup.config import backup_options, parse_target
from app.ops.backup.contracts import BackupError


def module():
    assert importlib.util.find_spec("app.ops.backup.tools") is not None, "PostgreSQL adapters are missing"
    return importlib.import_module("app.ops.backup.tools")


class ControlledProcesses:
    """模拟不可用的本机工具/容器边界；文件和适配器本身保持真实。"""
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.owner = ""
        self.fail_dump = False
        self.bad_label = False
        self.missing_image = False
        self.restored = b""
        self.private_passfile = False

    def __call__(self, argv, *, env, timeout, stdin=None, stdout=None):
        argv = list(argv)
        self.commands.append(argv)
        assert "a:b\\c" not in " ".join(argv)
        assert "DANG_TUSHARE_TOKEN" not in env
        if argv[1:3] == ["image", "inspect"]:
            if self.missing_image:
                raise BackupError("tool_failed", 3, "tools")
            return b"[{}]"
        if argv[1] == "create":
            label = argv[argv.index("--label") + 1]
            self.owner = label.partition("=")[2]
            self.private_passfile = False
            return ("a" * 64 + "\n").encode()
        if argv[1] == "inspect":
            return json.dumps([{"Id": "a" * 64, "Config": {"Labels": {"smartparse.backup.owner": "wrong" if self.bad_label else self.owner}}}]).encode()
        if argv[1] in {"start", "rm"}:
            return b""
        if argv[-1] == "cp /credentials/pgpass /tmp/pgpass && chmod 0600 /tmp/pgpass":
            self.private_passfile = True
            return b""
        if "--version" in argv:
            name = "pg_dump" if any("pg_dump" in value for value in argv) else "pg_restore"
            return (name + " (PostgreSQL) 17.11\n").encode()
        if "--list" in argv:
            return b"valid controlled list"
        if any("pg_dump" in value for value in argv):
            if "PGPASSFILE=/tmp/pgpass" in argv and not self.private_passfile:
                raise BackupError("controlled_passfile_not_ready", 3, "tools")
            if self.fail_dump:
                raise BackupError("tool_timeout", 3, "tools")
            assert stdout is not None
            stdout.write(b"\x00\xff\r\n")
            return b""
        if any("pg_restore" in value for value in argv):
            if "PGPASSFILE=/tmp/pgpass" in argv and not self.private_passfile:
                raise BackupError("controlled_passfile_not_ready", 3, "tools")
            assert stdin is not None
            self.restored = stdin.read()
            return b""
        raise AssertionError(f"unexpected controlled command: {argv[1:3]}")


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_binary_roundtrip_with_spaced_unicode_paths(mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = module()
    fake = ControlledProcesses()
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(tmp_path / name))
    options = backup_options(Settings(_env_file=None, backup_tool_mode=mode))
    archive = tmp_path / "空 格" / "database.dump"
    archive.parent.mkdir()
    target = parse_target("postgresql://u:a%3Ab%5Cc@127.0.0.1/db?sslmode=require&connect_timeout=12")
    with tools.open_tools(options) as adapter:
        versions = adapter.versions()
        tools.ensure_compatible_versions(versions, 17)
        adapter.dump(target, "00000001-00000002-1", archive, 10)
        adapter.list_archive(archive, 10)
        adapter.restore(target, archive, 10)
    assert archive.read_bytes() == b"\x00\xff\r\n"
    assert fake.restored == b"\x00\xff\r\n"
    dump = next(command for command in fake.commands if "--format=custom" in command)
    assert "--snapshot=00000001-00000002-1" in dump
    restore = next(command for command in fake.commands if "--single-transaction" in command)
    assert "--no-owner" in restore and "--no-acl" in restore
    assert "--clean" not in restore and "--create" not in restore


def test_windows_mount_passfile_copied_chmod_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = module()
    fake = ControlledProcesses()
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with tools.open_tools(backup_options(Settings(_env_file=None))) as adapter:
        adapter.dump(parse_target("postgresql://u:p@localhost/db"), "1-2-3", tmp_path / "database.dump", 10)
    create = next(command for command in fake.commands if command[1] == "create")
    assert "--pull=never" in create
    assert fake.private_passfile, "copy/chmod must finish before tool invocation"
    mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
    assert all("readonly" in mount for mount in mounts)
    assert not any("docker.sock" in arg or ".env" in arg or "danginvest-postgres" in arg for arg in create)
    assert any("PGPASSFILE=/tmp/pgpass" in command for command in fake.commands)


def test_timeout_kills_owned_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = module()
    fake = ControlledProcesses()
    fake.fail_dump = True
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError):
        with tools.open_tools(backup_options(Settings(_env_file=None))) as adapter:
            adapter.dump(parse_target("postgresql://u:p@localhost/db"), "1-2-3", tmp_path / "database.dump", 1)
    assert any(command[1:4] == ["rm", "--force", "--volumes"] and command[-1] == "a" * 64 for command in fake.commands)


def test_cleanup_label_mismatch_preserves_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = module()
    fake = ControlledProcesses()
    fake.bad_label = True
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError) as caught:
        with tools.open_tools(backup_options(Settings(_env_file=None))) as adapter:
            adapter.versions()
    assert caught.value.exit_code == 2
    assert not any(command[1] == "rm" for command in fake.commands)


def test_major_mismatch_and_missing_image_stop_before_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = module()
    with pytest.raises(BackupError) as caught:
        tools.ensure_compatible_versions(("pg_dump (PostgreSQL) 16.4", "pg_restore (PostgreSQL) 17.11"), 17)
    assert caught.value.exit_code == 2
    fake = ControlledProcesses()
    fake.missing_image = True
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError) as caught:
        with tools.open_tools(backup_options(Settings(_env_file=None))) as adapter:
            adapter.versions()
    assert caught.value.reason_code == "backup_image_unavailable"
    assert not any(command[1] in {"create", "pull"} for command in fake.commands)


def test_missing_native_tool_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = module()
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    with pytest.raises(BackupError) as caught:
        with tools.open_tools(backup_options(Settings(_env_file=None, backup_tool_mode="native"))):
            pytest.fail("missing native tools were ignored")
    assert caught.value.reason_code == "native_tools_unavailable"


class LostCreateResponse(ControlledProcesses):
    """Docker边界已创建对象，但CLI未收到ID；只模拟外部守护进程。"""
    def __init__(self, proof="owned", response="timeout"):
        super().__init__()
        self.proof, self.response = proof, response
        self.name = ""
        self.exists = False

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if argv[1] == "create":
            self.commands.append(argv)
            self.owner = argv[argv.index("--label") + 1].partition("=")[2]
            self.name = argv[argv.index("--name") + 1] if "--name" in argv else "unnamed"
            self.exists = True
            if self.response == "timeout":
                raise BackupError("tool_timeout", 3, "tools")
            return b"invalid-id\n"
        if argv[1] == "inspect" and argv[-1] == self.name:
            self.commands.append(argv)
            if self.proof == "unavailable":
                raise BackupError("tool_failed", 3, "tools")
            if self.proof == "absent":
                return b"[]"
            record = {"Id": "a" * 64, "Name": "/" + self.name,
                      "Config": {"Image": "postgres:17-alpine", "Labels": {"smartparse.backup.owner": self.owner}}}
            if self.proof == "label":
                record["Config"]["Labels"]["smartparse.backup.owner"] = "another-operation"
            elif self.proof == "name":
                record["Name"] = "/another-container"
            elif self.proof == "image":
                record["Config"]["Image"] = "unrelated:latest"
            elif self.proof == "multiple":
                return json.dumps([record, record]).encode()
            return json.dumps([record]).encode()
        if argv[1] == "rm":
            assert argv[-1] == "a" * 64
            self.exists = False
        return super().__call__(argv, **kwargs)


@pytest.mark.parametrize("response", ["timeout", "invalid-id"])
def test_lost_create_id_recovers_exact_owned_helper_without_starting_it(response, monkeypatch):
    tools = module()
    fake = LostCreateResponse(response=response)
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError):
        with tools.helper_container(backup_options(Settings(_env_file=None))):
            pytest.fail("create failure must not enter tool body")
    assert not fake.exists, "owned helper leaked when create did not return a usable ID"
    assert not any(command[1] == "start" for command in fake.commands)


@pytest.mark.parametrize("proof", ["label", "name", "image", "multiple", "unavailable", "absent"])
def test_lost_create_id_never_removes_unconfirmed_helper(proof, monkeypatch):
    tools = module()
    fake = LostCreateResponse(proof=proof)
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError) as caught:
        with tools.helper_container(backup_options(Settings(_env_file=None))):
            pytest.fail("create failure must not enter tool body")
    assert caught.value.reason_code == "tool_timeout"
    assert fake.exists
    assert not any(command[1] in {"start", "rm"} for command in fake.commands)


@pytest.mark.parametrize("phase", ["inspect", "remove", "budget-entry", "budget-exit"])
def test_cleanup_failure_does_not_replace_original_create_exception(phase, monkeypatch, caplog):
    tools = module()
    fake = LostCreateResponse()
    primary = BackupError("tool_timeout", 3, "tools")

    def controlled(argv, **kwargs):
        if argv[1] == "create":
            try:
                return fake(argv, **kwargs)
            except BackupError:
                raise primary
        if phase == "inspect" and argv[1] == "inspect" and argv[-1] == "a" * 64:
            raise BackupError("tool_failed", 3, "tools")
        if phase == "remove" and argv[1] == "rm":
            raise BackupError("tool_failed", 3, "tools")
        return fake(argv, **kwargs)

    if phase.startswith("budget"):
        @contextmanager
        def broken_budget():
            if phase == "budget-entry":
                raise BackupError("backup_budget_exhausted", 3, "timeout")
            yield
            raise BackupError("backup_budget_exhausted", 3, "timeout")
        monkeypatch.setattr(tools, "cleanup_budget", broken_budget)
    monkeypatch.setattr(tools, "run_process", controlled)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError) as caught:
        with tools.helper_container(backup_options(Settings(_env_file=None))):
            pytest.fail("create failure must not enter body")
    assert caught.value is primary
    assert caught.value.reason_code == "tool_timeout" and caught.value.exit_code == 3
    if phase in {"inspect", "budget-entry"}:
        assert not any(command[1] == "rm" for command in fake.commands)
    assert any(json.loads(record.getMessage()).get("reasonCode") == "backup_helper_cleanup_failed"
               for record in caplog.records)


def test_cleanup_failure_after_successful_body_is_not_silenced(monkeypatch):
    tools = module()
    fake = ControlledProcesses()
    failure = BackupError("tool_failed", 3, "tools")
    def controlled(argv, **kwargs):
        if argv[1] == "rm":
            raise failure
        return fake(argv, **kwargs)
    monkeypatch.setattr(tools, "run_process", controlled)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError) as caught:
        with tools.helper_container(backup_options(Settings(_env_file=None))):
            pass
    assert caught.value is failure
