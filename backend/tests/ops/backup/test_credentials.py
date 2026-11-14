from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import stat

import pytest

from app.ops.backup.config import parse_target
from app.ops.backup.contracts import BackupError


def module():
    assert importlib.util.find_spec("app.ops.backup.credentials") is not None, "temporary passfile is missing"
    return importlib.import_module("app.ops.backup.credentials")


def test_password_fields_are_escaped_and_removed() -> None:
    credentials = module()
    target = parse_target("postgresql://u:a%3Ab%5Cc@127.0.0.1/db")
    with credentials.temporary_passfile(target) as path:
        assert path.read_text(encoding="utf-8") == "127.0.0.1:5432:db:u:a\\:b\\\\c\n"
        assert "a:b" not in str(path)
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        else:
            # 查询真正的 DACL，不用 chmod 假装 Windows 已安全。
            assert credentials.windows_private_acl(path.parent)
    assert not path.exists()
    assert not path.parent.exists()


def test_permissions_failure_blocks_before_password_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = module()
    monkeypatch.setattr(credentials.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    def denied(path: Path) -> None:
        raise OSError("secret-canary")
    monkeypatch.setattr(credentials, "restrict_directory", denied)
    with pytest.raises(BackupError) as caught:
        with credentials.temporary_passfile(parse_target("postgresql://u:secret-canary@host/db")):
            pytest.fail("permission failure was ignored")
    assert caught.value.exit_code == 2
    assert "secret-canary" not in str(caught.value)
    assert not (tmp_path / "pgpass").exists()


def test_exception_still_removes_owned_credentials() -> None:
    credentials = module()
    with pytest.raises(RuntimeError):
        with credentials.temporary_passfile(parse_target("postgresql://u:p@host/db")) as path:
            raise RuntimeError("controlled")
    assert not path.exists()
