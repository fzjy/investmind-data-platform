from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.ops.backup.config import backup_options
from app.ops.backup.contracts import BackupError


def module():
    assert importlib.util.find_spec("app.ops.backup.targets") is not None, "owned restore target is missing"
    return importlib.import_module("app.ops.backup.targets")


class ControlledTargetProcesses:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.owner = ""
        self.bad_label = False

    def __call__(self, argv, *, env, timeout, **kwargs):
        argv = list(argv)
        self.commands.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return b"[{}]"
        if argv[1] == "create":
            self.owner = argv[argv.index("--label") + 1].split("=", 1)[1]
            return ("b" * 64).encode()
        if argv[1] == "inspect":
            return json.dumps([{"Id": "b" * 64, "Config": {"Labels": {"smartparse.backup.owner": "wrong" if self.bad_label else self.owner}}, "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "35432"}]}}}]).encode()
        if argv[1] in {"start", "rm", "exec"}:
            return b""
        raise AssertionError("unexpected controlled operation")


def test_source_container_never_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    targets = module()
    import app.ops.backup.tools as tools
    fake = ControlledTargetProcesses()
    monkeypatch.setattr(targets, "run_process", fake)
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with targets.open_restore_target(backup_options(Settings(_env_file=None))) as restored:
        assert restored.target.host == "127.0.0.1"
        assert restored.target.port == 35432
        assert restored.target.database == "smartparse_restore"
        assert restored.container_id == "b" * 64
        assert restored.owner_token == fake.owner
        password = restored.target.password.get_secret_value()
        assert len(password) >= 32
        assert password not in json.dumps(fake.commands)
    create = next(command for command in fake.commands if command[1] == "create")
    assert "127.0.0.1::5432" in create and "--pull=never" in create
    assert not any("danginvest-postgres" in argument or "--volume" in argument for argument in create)
    assert any(command[-1] == "b" * 64 and command[1] == "rm" for command in fake.commands)


def test_cleanup_label_mismatch_preserves_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    targets = module()
    import app.ops.backup.tools as tools
    fake = ControlledTargetProcesses()
    fake.bad_label = True
    monkeypatch.setattr(targets, "run_process", fake)
    monkeypatch.setattr(tools, "run_process", fake)
    monkeypatch.setattr(tools.shutil, "which", lambda name: name)
    with pytest.raises(BackupError):
        with targets.open_restore_target(backup_options(Settings(_env_file=None))):
            pytest.fail("foreign label accepted")
    assert not any(command[1] == "rm" for command in fake.commands)
