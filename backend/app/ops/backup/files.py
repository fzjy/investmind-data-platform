from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO, Iterator
import uuid

from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.ops.backup.contracts import BackupError, BackupManifest, BackupResult
from app.ops.backup.budget import check_budget, cleanup_budget


ID_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{32}\Z")
PACKAGE_MEMBERS = frozenset({"database.dump", "manifest.json", "verification.json"})
MAX_JSON_BYTES = 4 * 1024 * 1024


def _error(reason: str = "unsafe_backup_path", code: int = 2) -> BackupError:
    return BackupError(reason, code, "storage")


def _check_path(path: Path) -> None:
    """在 resolve 或打开之前检查所有现存祖先，不跟随链接。"""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _error()
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise _error()


def _root_path(root: Path) -> Path:
    if ".." in root.parts:
        raise _error()
    absolute = Path(os.path.abspath(root))
    _check_path(absolute)
    project = PROJECT_ROOT.resolve()
    home = Path.home().resolve()
    if absolute == Path(absolute.anchor) or absolute in (home, *home.parents, project, *project.parents):
        raise _error()
    protected = (
        ".git", ".codex", ".agents", ".claude", "src", "node_modules", "scripts", "public", "docs",
        "backend/app", "backend/tests", "backend/migrations", "backend/config", "backend/.venv",
    )
    if absolute == project / "backend" or any(absolute.is_relative_to(project / name) for name in protected):
        raise _error()
    if absolute.exists() and not absolute.is_dir():
        raise _error()
    if absolute.exists() and any((absolute / name).exists() for name in (".git", "AGENTS.md", "pyproject.toml", "package.json")):
        raise _error()
    return absolute


def _identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
        raise _error("invalid_backup_id")
    try:
        datetime.strptime(identifier[:16], "%Y%m%dT%H%M%SZ")
    except ValueError:
        raise _error("invalid_backup_id") from None
    return identifier


@contextmanager
def _pin_directory(path: Path) -> Iterator[None]:
    """Windows 不共享 DELETE，防止操作期间目录被重命名/替换为 junction。"""
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # OPEN_REPARSE_POINT | BACKUP_SEMANTICS，检查后也不跟随新出现的 reparse。
    handle = kernel.CreateFileW(str(path), 0, 3, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise _error()
    try:
        _check_path(path)
        yield
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _guard(path: Path) -> Iterator[None]:
    _check_path(path)
    with ExitStack() as stack:
        for parent in (*reversed(path.parents), path):
            if parent.is_dir():
                stack.enter_context(_pin_directory(parent))
        _check_path(path)
        yield
        _check_path(path)


@contextmanager
def _open_file(path: Path, *, write: bool = False) -> Iterator[BinaryIO]:
    _check_path(path)
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r+b" if write else "rb") as stream:
        before = os.fstat(stream.fileno())
        after = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise _error()
        _check_path(path)
        yield stream


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _guard(path.parent), _open_file(path) as stream:
        while True:
            check_budget()
            chunk = stream.read(1024 * 1024)
            check_budget()
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(directory: Path) -> None:
    # Windows 无可移植目录 fsync；文件 fsync + 同卷原子 rename，文档如实说明。
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _delete_posix_package(path: Path, members: frozenset[str]) -> None:
    """逐级 no-follow 打开；检查后父目录被替换也不能把 unlink 导向外部。"""
    descriptors: list[int] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path.anchor, flags)
        descriptors.append(descriptor)
        for part in path.parent.parts[1:]:
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        root_fd = descriptor
        package_fd = os.open(path.name, flags, dir_fd=root_fd)
        descriptors.append(package_fd)
        pinned = os.fstat(package_fd)

        def unchanged() -> None:
            current = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)):
                raise _error()

        unchanged()
        if frozenset(os.listdir(package_fd)) != members or members - PACKAGE_MEMBERS:
            raise _error("unknown_package_members")
        for name in sorted(members, key=lambda member: member == "manifest.json"):
            unchanged()
            info = os.stat(name, dir_fd=package_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _error()
            os.unlink(name, dir_fd=package_fd)
        unchanged()
        os.rmdir(path.name, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_json(path: Path) -> bytes:
    check_budget()
    with _guard(path.parent), _open_file(path) as stream:
        value = stream.read(MAX_JSON_BYTES + 1)
    check_budget()
    if len(value) > MAX_JSON_BYTES:
        raise _error("invalid_manifest", 4)
    return value


def _write_new(path: Path, content: bytes) -> None:
    check_budget()
    with _guard(path.parent):
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
        check_budget()


def _manifest_time(manifest: BackupManifest) -> None:
    completed = manifest.completed_at
    if completed.utcoffset() is None or completed.utcoffset().total_seconds() != 0:
        raise _error("invalid_manifest_time", 4)
    created = datetime.strptime(manifest.backup_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if completed < created:
        raise _error("invalid_manifest_time", 4)
    if completed > datetime.now(timezone.utc):
        raise _error("future_manifest_time", 2)


class BackupStore:
    def __init__(self, root: Path) -> None:
        self.root = _root_path(root)
        self.anomaly_count = 0

    def _package(self, backup_id: str, *, partial: bool = False) -> Path:
        identifier = _identifier(backup_id)
        _root_path(self.root)
        path = self.root / ((".partial-" if partial else "") + identifier)
        _check_path(path)
        return path

    def _members(self, path: Path) -> frozenset[str]:
        _check_path(path)
        if not path.is_dir():
            raise _error("missing_backup", 4)
        members = frozenset(child.name for child in path.iterdir())
        if members - PACKAGE_MEMBERS:
            raise _error("unknown_package_members")
        for name in members:
            _check_path(path / name)
            if not (path / name).is_file():
                raise _error()
        return members

    def scan(self) -> tuple[BackupManifest, ...]:
        _root_path(self.root)
        self.anomaly_count = 0
        if not self.root.exists():
            return ()
        packages: list[BackupManifest] = []
        with _guard(self.root):
            for path in sorted(self.root.iterdir()):
                if path.name == ".backup.lock":
                    _check_path(path)
                    continue
                if not ID_PATTERN.fullmatch(path.name):
                    self.anomaly_count += 1
                    continue
                try:
                    packages.append(self.load(path.name))
                except BackupError as error:
                    if error.reason_code == "future_manifest_time":
                        raise
                    self.anomaly_count += 1
        return tuple(sorted(packages, key=lambda package: (package.completed_at, package.backup_id)))

    def load(self, backup_id: str) -> BackupManifest:
        path = self._package(backup_id)
        try:
            with _guard(path):
                if not {"database.dump", "manifest.json"}.issubset(self._members(path)):
                    raise _error("incomplete_backup", 4)
                data = _read_json(path / "manifest.json")
                raw = json.loads(data)
                if not isinstance(raw, dict) or type(raw.get("formatVersion")) is not int:
                    raise _error("invalid_manifest", 4)
                manifest = BackupManifest.model_validate_json(data)
                if manifest.backup_id != backup_id:
                    raise _error("manifest_id_mismatch", 4)
                _manifest_time(manifest)
                dump = path / "database.dump"
                if dump.stat().st_size != manifest.archive_size or file_sha256(dump) != manifest.archive_sha256:
                    raise _error("archive_checksum_mismatch", 4)
                return manifest
        except (OSError, ValueError, ValidationError):
            raise _error("invalid_backup_package", 4) from None

    def stage(self, backup_id: str) -> Path:
        stage = self._package(backup_id, partial=True)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with _guard(self.root):
                if self._package(backup_id).exists():
                    raise _error("backup_id_exists")
                stage.mkdir()
            return stage
        except OSError:
            raise _error("backup_stage_failed", 3) from None

    def promote(self, backup_id: str, manifest: BackupManifest) -> Path:
        stage = self._package(backup_id, partial=True)
        destination = self._package(backup_id)
        try:
            with _guard(self.root):
                if destination.exists() or manifest.backup_id != backup_id:
                    raise _error("backup_id_exists")
                _manifest_time(manifest)
                if self._members(stage) != {"database.dump"}:
                    raise _error("invalid_staging_package", 4)
                dump = stage / "database.dump"
                if dump.stat().st_size != manifest.archive_size or file_sha256(dump) != manifest.archive_sha256:
                    raise _error("archive_checksum_mismatch", 4)
                with _open_file(dump, write=True) as stream:
                    os.fsync(stream.fileno())
                _write_new(stage / "manifest.json", manifest.model_dump_json(by_alias=True).encode("utf-8"))
                check_budget()
                # UUID ID + 互斥锁；拒绝已存在目的地，Windows rename 本身也拒绝覆盖。
                stage.rename(destination)
                try:
                    _sync_directory(self.root)
                    check_budget()
                except (BackupError, OSError):
                    # 落盘跨过期限不能留下被扫描成完整成功的包；保留 partial 供排查。
                    with cleanup_budget():
                        _check_path(stage)
                        _check_path(destination)
                        if stage.exists():
                            raise _error("backup_rollback_blocked", 3)
                        destination.rename(stage)
                        _sync_directory(self.root)
                    raise
            return destination
        except OSError:
            raise _error("backup_publish_failed", 3) from None

    def write_verification(self, backup_id: str, report: BackupResult) -> None:
        path = self._package(backup_id)
        with _guard(path):
            manifest = self.load(backup_id)
            if (report.backup_id != backup_id or report.status != "completed"
                    or report.verification_level != "restore_verified"
                    or report.archive_sha256 != manifest.archive_sha256
                    or report.manifest_sha256 != file_sha256(path / "manifest.json")
                    or report.ended_at.tzinfo is None or report.ended_at > datetime.now(timezone.utc)):
                raise _error("invalid_verification_binding", 4)
            temporary = path / (".verification-" + uuid.uuid4().hex + ".tmp")
            report_path = path / "verification.json"
            previous = _read_json(report_path) if report_path.exists() else None
            replaced = False
            try:
                _write_new(temporary, report.model_dump_json(by_alias=True).encode("utf-8"))
                _check_path(path / "verification.json")
                check_budget()
                os.replace(temporary, report_path)
                replaced = True
                _sync_directory(path)
                check_budget()
            except (BackupError, OSError):
                if replaced:
                    with cleanup_budget():
                        _check_path(report_path)
                        if previous is None:
                            report_path.unlink()
                        else:
                            _write_new(temporary, previous)
                            os.replace(temporary, report_path)
                        _sync_directory(path)
                raise
            finally:
                if temporary.exists():
                    temporary.unlink()

    def read_verification(self, backup_id: str) -> BackupResult | None:
        path = self._package(backup_id)
        manifest = self.load(backup_id)
        report_path = path / "verification.json"
        _check_path(report_path)
        if not report_path.exists():
            return None
        try:
            report = BackupResult.model_validate_json(_read_json(report_path))
            if (report.backup_id != backup_id or report.status != "completed" or report.stage != "verification"
                    or report.verification_level != "restore_verified" or report.archive_sha256 != manifest.archive_sha256
                    or report.manifest_sha256 != file_sha256(path / "manifest.json")
                    or report.ended_at.utcoffset() is None or report.ended_at.utcoffset().total_seconds() != 0
                    or report.started_at.utcoffset() is None or report.ended_at < report.started_at
                    or report.ended_at < manifest.completed_at or report.ended_at > datetime.now(timezone.utc)):
                return None
            return report
        except (OSError, ValueError, ValidationError, BackupError):
            return None

    def delete_package(self, backup_id: str) -> None:
        path = self._package(backup_id)
        with _guard(self.root):
            # 不在 Windows 删除时持有该目录自己的禁止 DELETE 句柄。
            with _guard(path):
                self.load(backup_id)
                members = self._members(path)
                if os.name != "nt":
                    _delete_posix_package(path, members)
                    return
                for name in sorted(members, key=lambda member: member == "manifest.json"):
                    _check_path(path)
                    _check_path(path / name)
                    (path / name).unlink()
            _check_path(path)
            path.rmdir()
            _sync_directory(self.root)


@contextmanager
def backup_lock(root: Path) -> Iterator[None]:
    root = _root_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with _guard(root):
        lock_path = root / ".backup.lock"
        _check_path(lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            _check_path(lock_path)
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BackupError("backup_busy", 5, "lock") from None
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
