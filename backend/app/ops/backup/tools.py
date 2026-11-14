from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Iterator, Protocol
import uuid

from app.ops.backup.contracts import BackupError, BackupOptions, ConnectionTarget
from app.ops.backup.credentials import temporary_passfile
from app.ops.backup.process import run_process, runtime_environment
from app.ops.backup.budget import cleanup_budget, deadline_scope, remaining_timeout


OWNER_LABEL = "smartparse.backup.owner"


def _run(argv, *, timeout: float, **kwargs) -> bytes:
    return run_process(argv, timeout=remaining_timeout(timeout), **kwargs)


class PgTools(Protocol):
    def versions(self) -> tuple[str, str]: ...
    def dump(self, target: ConnectionTarget, snapshot_id: str, destination: Path, timeout: float) -> None: ...
    def list_archive(self, archive: Path, timeout: float) -> None: ...
    def restore(self, target: ConnectionTarget, archive: Path, timeout: float) -> None: ...


def ensure_compatible_versions(versions: tuple[str, str], server_major: int) -> None:
    for version, tool in zip(versions, ("pg_dump", "pg_restore"), strict=True):
        match = re.fullmatch(re.escape(tool) + r" \(PostgreSQL\) (\d+)\.\d+(?:[ .].*)?", version)
        if match is None or int(match[1]) != server_major:
            raise BackupError("postgres_major_mismatch", 2, "preflight")


def _arguments(target: ConnectionTarget) -> list[str]:
    return ["--host", target.host, "--port", str(target.port), "--username", target.username, "--dbname", target.database, "--no-password"]


def _environment(target: ConnectionTarget, passfile: Path) -> dict[str, str]:
    return {**runtime_environment(), "PGPASSFILE": str(passfile), "PGSSLMODE": target.sslmode, "PGCONNECT_TIMEOUT": str(target.connect_timeout)}


class NativeTools:
    def __init__(self, options: BackupOptions) -> None:
        suffix = ".exe" if os.name == "nt" else ""
        self.executables: dict[str, str] = {}
        self.timeout = options.timeout_seconds
        for tool in ("pg_dump", "pg_restore"):
            if options.native_bin is None:
                path = shutil.which(tool)
            else:
                candidate = options.native_bin / (tool + suffix)
                path = str(candidate) if candidate.is_file() else None
            if path is None:
                raise BackupError("native_tools_unavailable", 2, "preflight")
            self.executables[tool] = path

    def versions(self) -> tuple[str, str]:
        with deadline_scope(self.timeout):
            values = [_run([self.executables[tool], "--version"], env=runtime_environment(), timeout=self.timeout).decode("utf-8").strip() for tool in ("pg_dump", "pg_restore")]
        return values[0], values[1]

    def dump(self, target: ConnectionTarget, snapshot_id: str, destination: Path, timeout: float) -> None:
        with deadline_scope(timeout), temporary_passfile(target) as passfile, destination.open("xb") as stream:
            _run([self.executables["pg_dump"], "--format=custom", "--snapshot=" + snapshot_id, *_arguments(target)], env=_environment(target, passfile), timeout=timeout, stdout=stream)

    def list_archive(self, archive: Path, timeout: float) -> None:
        # TOC 可很大，只判断退出码，丢弃内容而不是无界读取或打印。
        with deadline_scope(timeout), open(os.devnull, "wb") as output:
            _run([self.executables["pg_restore"], "--list", str(archive)], env=runtime_environment(), timeout=timeout, stdout=output)

    def restore(self, target: ConnectionTarget, archive: Path, timeout: float) -> None:
        with deadline_scope(timeout), temporary_passfile(target) as passfile, archive.open("rb") as stream:
            _run([self.executables["pg_restore"], "--single-transaction", "--no-owner", "--no-acl", "--exit-on-error", *_arguments(target)], env=_environment(target, passfile), timeout=timeout, stdin=stream)


def docker_executable() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise BackupError("docker_unavailable", 2, "preflight")
    return executable


def assert_owned_container(executable: str, identifier: str, owner: str) -> None:
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError
        data = json.loads(_run([executable, "inspect", identifier], env=runtime_environment(), timeout=30))
        if len(data) != 1 or data[0]["Id"] != identifier or data[0]["Config"]["Labels"].get(OWNER_LABEL) != owner:
            raise ValueError
    except (ValueError, KeyError, TypeError, BackupError):
        raise BackupError("container_ownership_unconfirmed", 2, "cleanup") from None


def _find_owned_helper(executable: str, name: str, owner: str, image: str) -> str | None:
    """仅用本次随机名称找回丢失ID；无法证明归属则不删除。"""
    try:
        records = json.loads(_run([executable, "inspect", name], env=runtime_environment(), timeout=30))
        if len(records) != 1:
            return None
        record = records[0]
        identifier = record["Id"]
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier)
                or record["Name"] != "/" + name or record["Config"]["Image"] != image
                or record["Config"]["Labels"].get(OWNER_LABEL) != owner):
            return None
        return identifier
    except (ValueError, KeyError, TypeError, AttributeError, BackupError):
        return None


@contextmanager
def helper_container(options: BackupOptions, *, directory: Path | None = None, passfile: Path | None = None) -> Iterator[tuple[str, str]]:
    executable = docker_executable()
    try:
        _run([executable, "image", "inspect", options.docker_image], env=runtime_environment(), timeout=30)
    except BackupError:
        raise BackupError("backup_image_unavailable", 2, "preflight") from None
    owner = uuid.uuid4().hex
    name = "smartparse-backup-helper-" + owner
    command = [executable, "create", "--pull=never", "--name", name, "--label", OWNER_LABEL + "=" + owner,
               "--add-host=host.docker.internal:host-gateway", "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid"]
    if directory is not None:
        if "," in str(directory):
            raise BackupError("unsupported_docker_mount_path", 2, "preflight")
        command += ["--mount", f"type=bind,source={directory},target=/backup,readonly"]
    if passfile is not None:
        command += ["--mount", f"type=bind,source={passfile},target=/credentials/pgpass,readonly"]
    startup = "exec sleep 7200"
    command += [options.docker_image, "sh", "-c", startup]
    identifier = None
    primary_error: BaseException | None = None
    try:
        raw_identifier = _run(command, env=runtime_environment(), timeout=30)
        try:
            candidate = raw_identifier.decode("ascii").strip()
        except UnicodeDecodeError:
            candidate = ""
        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
            raise BackupError("container_ownership_unconfirmed", 2, "cleanup")
        identifier = candidate
        assert_owned_container(executable, identifier, owner)
        _run([executable, "start", identifier], env=runtime_environment(), timeout=30)
        if passfile is not None:
            _run([executable, "exec", identifier, "sh", "-c", "cp /credentials/pgpass /tmp/pgpass && chmod 0600 /tmp/pgpass"], env=runtime_environment(), timeout=30)
        yield executable, identifier
    except BaseException as error:
        primary_error = error
        raise
    finally:
        # 无法确认所有权时保留，不猜测或删除其他资源。
        try:
            with cleanup_budget():
                if identifier is None:
                    identifier = _find_owned_helper(executable, name, owner, options.docker_image)
                if identifier is not None:
                    assert_owned_container(executable, identifier, owner)
                    _run([executable, "rm", "--force", "--volumes", identifier], env=runtime_environment(), timeout=30)
        except Exception:
            if primary_error is None:
                raise
            # 保留主异常身份/错误码，不把清理失败冒充业务配置错误。
            # 只发常量告警，不记录异常文本、命令、容器挂载或凭据。
            primary_error.add_note("backup_helper_cleanup_failed")
            try:
                logging.getLogger(__name__).critical(json.dumps({
                    "event": "safety_alert", "reasonCode": "backup_helper_cleanup_failed",
                }), extra={"alert": "backup_helper_cleanup_failed"})
            except Exception:
                pass  # 外部日志收集器故障也不能覆盖主异常。


def _docker_target(target: ConnectionTarget) -> ConnectionTarget:
    if target.host in {"localhost", "127.0.0.1", "::1"}:
        if target.sslmode == "verify-full":
            # 改 hostname 会改变证书核验语义；明确拒绝而不是降级 TLS。
            raise BackupError("docker_loopback_tls_hostname_unsupported", 2, "preflight")
        return target.model_copy(update={"host": "host.docker.internal"})
    return target


class DockerTools:
    def __init__(self, options: BackupOptions) -> None:
        self.options = options

    def versions(self) -> tuple[str, str]:
        with deadline_scope(self.options.timeout_seconds), helper_container(self.options) as (executable, identifier):
            values = [_run([executable, "exec", identifier, tool, "--version"], env=runtime_environment(), timeout=self.options.timeout_seconds).decode("utf-8").strip() for tool in ("pg_dump", "pg_restore")]
        return values[0], values[1]

    @contextmanager
    def _connected(self, target: ConnectionTarget, directory: Path) -> Iterator[list[str]]:
        target = _docker_target(target)
        with temporary_passfile(target) as passfile:
            with helper_container(self.options, directory=directory, passfile=passfile) as (executable, identifier):
                yield [executable, "exec", "-i", "--env", "PGPASSFILE=/tmp/pgpass", "--env", "PGSSLMODE=" + target.sslmode, "--env", "PGCONNECT_TIMEOUT=" + str(target.connect_timeout), identifier]

    def dump(self, target: ConnectionTarget, snapshot_id: str, destination: Path, timeout: float) -> None:
        translated = _docker_target(target)
        with deadline_scope(timeout), self._connected(target, destination.parent) as prefix, destination.open("xb") as stream:
            _run([*prefix, "pg_dump", "--format=custom", "--snapshot=" + snapshot_id, *_arguments(translated)], env=runtime_environment(), timeout=timeout, stdout=stream)

    def list_archive(self, archive: Path, timeout: float) -> None:
        with deadline_scope(timeout), helper_container(self.options, directory=archive.parent) as (executable, identifier), open(os.devnull, "wb") as output:
            _run([executable, "exec", identifier, "pg_restore", "--list", "/backup/" + archive.name], env=runtime_environment(), timeout=timeout, stdout=output)

    def restore(self, target: ConnectionTarget, archive: Path, timeout: float) -> None:
        translated = _docker_target(target)
        with deadline_scope(timeout), self._connected(target, archive.parent) as prefix, archive.open("rb") as stream:
            _run([*prefix, "pg_restore", "--single-transaction", "--no-owner", "--no-acl", "--exit-on-error", *_arguments(translated)], env=runtime_environment(), timeout=timeout, stdin=stream)


@contextmanager
def open_tools(options: BackupOptions) -> Iterator[PgTools]:
    if options.tool_mode == "native":
        yield NativeTools(options)
    else:
        yield DockerTools(options)
