from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel


BackupId = Annotated[str, Field(pattern=r"^\d{8}T\d{6}Z-[0-9a-f]{32}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ToolMode = Literal["native", "docker"]
VerificationLevel = Literal["none", "archive_valid", "restore_verified"]


class Contract(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True,
        alias_generator=to_camel, populate_by_name=True,
    )


class BackupOptions(Contract):
    tool_mode: ToolMode
    path: Path
    native_bin: Path | None
    docker_image: str
    interval_days: int = Field(ge=1, le=365)
    keep_count: int = Field(ge=2, le=100)
    retention_days: int = Field(ge=1, le=3650)
    timeout_seconds: int = Field(ge=60, le=7200)


class ConnectionTarget(Contract):
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str
    username: str
    password: SecretStr = Field(repr=False, exclude=True)
    sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    connect_timeout: int = Field(default=10, gt=0)


class TableInventory(Contract):
    schema_name: str
    table_name: str
    row_count: int = Field(ge=0)
    rows_sha256: Sha256
    schema_sha256: Sha256


class PublicationInventory(Contract):
    product: str
    version: str
    row_count: int | None = Field(default=None, ge=0)
    stale: bool = False


class SequenceInventory(Contract):
    schema_name: str
    sequence_name: str
    table_schema: str
    table_name: str
    column_name: str
    maximum_key: int | None
    increment: int
    definition_sha256: Sha256 = "0" * 64


class DatabaseInventory(Contract):
    tables: tuple[TableInventory, ...]
    schemas: tuple[str, ...] = ()
    alembic_versions: tuple[str, ...] = ()
    publications: tuple[PublicationInventory, ...] = ()
    sequences: tuple[SequenceInventory, ...] = ()


class BackupManifest(Contract):
    format_version: Literal[1] = 1
    backup_id: BackupId
    source_identity: Sha256
    completed_at: datetime
    archive_sha256: Sha256
    archive_size: int = Field(gt=0)
    archive_format: Literal["custom"] = "custom"
    tool_mode: ToolMode
    dump_version: str
    restore_version: str
    server_major: int = Field(gt=0)
    code_head: str
    inventory: DatabaseInventory


class BackupResult(Contract):
    status: Literal["completed", "skipped", "blocked", "failed", "busy", "empty", "ready", "planned", "pruned"]
    stage: str
    reason_code: str
    backup_id: BackupId | None = None
    tool_mode: ToolMode
    started_at: datetime
    ended_at: datetime
    elapsed_seconds: float = Field(default=0, ge=0)
    archive_size: int = Field(default=0, ge=0)
    table_count: int = Field(default=0, ge=0)
    verification_level: VerificationLevel = "none"
    archive_sha256: Sha256 | None = None
    manifest_sha256: Sha256 | None = None
    candidates: tuple[BackupId, ...] = ()
    latest_verified_id: BackupId | None = None
    due: bool | None = None
    anomaly_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    verification_expired: bool | None = None


class BackupError(Exception):
    """仅允许内部稳定代码；不接收底层异常文本或用户输入。"""

    def __init__(self, reason_code: str, exit_code: int, stage: str, *, process_returncode: int | None = None) -> None:
        self.reason_code = reason_code
        self.exit_code = exit_code
        self.stage = stage
        self.process_returncode = process_returncode
        super().__init__(f"{stage}: {reason_code}")
