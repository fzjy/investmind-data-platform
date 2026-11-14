from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import secrets
import time
from typing import Iterator
import uuid

from pydantic import SecretStr

from app.ops.backup.contracts import BackupError, BackupOptions, ConnectionTarget
from app.ops.backup.credentials import temporary_passfile
from app.ops.backup.process import run_process, runtime_environment
from app.ops.backup.tools import OWNER_LABEL, assert_owned_container, docker_executable
from app.ops.backup.budget import cleanup_budget, deadline_scope, remaining_timeout


@dataclass(frozen=True)
class RestoreTarget:
    target: ConnectionTarget
    container_id: str
    owner_token: str

    def assert_owned(self) -> None:
        assert_owned_container(docker_executable(), self.container_id, self.owner_token)


def _run(argv, *, timeout: float, **kwargs) -> bytes:
    return run_process(argv, timeout=remaining_timeout(timeout), **kwargs)


@contextmanager
def open_restore_target(options: BackupOptions) -> Iterator[RestoreTarget]:
    with deadline_scope(options.timeout_seconds), _restore_target(options) as target:
        yield target


@contextmanager
def _restore_target(options: BackupOptions) -> Iterator[RestoreTarget]:
    executable = docker_executable()
    try:
        _run([executable, "image", "inspect", options.docker_image], env=runtime_environment(), timeout=30)
    except BackupError:
        raise BackupError("backup_image_unavailable", 2, "preflight") from None
    owner = uuid.uuid4().hex
    password = secrets.token_urlsafe(48)
    initial = ConnectionTarget(host="127.0.0.1", database="smartparse_restore", username="smartparse_restore", password=SecretStr(password), sslmode="disable")
    with temporary_passfile(initial) as passfile:
        password_file = passfile.parent / "postgres-password"
        try:
            with password_file.open("xb") as stream:
                stream.write(password.encode("ascii"))
                stream.flush()
            command = [executable, "create", "--pull=never", "--name", "smartparse-restore-" + owner,
                "--label", OWNER_LABEL + "=" + owner, "--publish", "127.0.0.1::5432",
                "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid",
                "--env", "POSTGRES_DB=smartparse_restore", "--env", "POSTGRES_USER=smartparse_restore",
                "--env", "POSTGRES_PASSWORD_FILE=/run/credentials/password",
                "--mount", f"type=bind,source={password_file},target=/run/credentials/password,readonly", options.docker_image]
            identifier = _run(command, env=runtime_environment(), timeout=30).decode("ascii").strip()
            try:
                assert_owned_container(executable, identifier, owner)
                _run([executable, "start", identifier], env=runtime_environment(), timeout=30)
                deadline = time.monotonic() + min(options.timeout_seconds, 60)
                while True:
                    try:
                        _run([executable, "exec", identifier, "pg_isready", "--username", initial.username, "--dbname", initial.database], env=runtime_environment(), timeout=min(5, max(0.001, deadline - time.monotonic())))
                        break
                    except BackupError:
                        remaining_timeout(5)
                        if time.monotonic() >= deadline:
                            raise BackupError("restore_target_not_ready", 3, "target") from None
                        time.sleep(0.2)
                assert_owned_container(executable, identifier, owner)
                try:
                    details = json.loads(_run([executable, "inspect", identifier], env=runtime_environment(), timeout=30))
                    ports = details[0]["NetworkSettings"]["Ports"]["5432/tcp"]
                    if len(ports) != 1 or ports[0]["HostIp"] != "127.0.0.1":
                        raise ValueError
                    port = int(ports[0]["HostPort"])
                    if not 1 <= port <= 65535:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise BackupError("unsafe_restore_target_binding", 2, "target") from None
                yield RestoreTarget(initial.model_copy(update={"port": port}), identifier, owner)
            finally:
                with cleanup_budget():
                    assert_owned_container(executable, identifier, owner)
                    _run([executable, "rm", "--force", "--volumes", identifier], env=runtime_environment(), timeout=30)
        finally:
            if password_file.exists():
                password_file.unlink()
