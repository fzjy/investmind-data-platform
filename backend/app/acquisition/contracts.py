from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True, order=True)
class StateKey:
    entry_id: str
    item_id: str
    subject_kind: str
    subject_key: str
    scope_kind: Literal['routine', 'historical'] = 'routine'


@dataclass(frozen=True)
class Attempt:
    key: StateKey
    attempt_id: str
    started_at: datetime
    scope: dict
    trigger: str


@dataclass(frozen=True)
class Evidence:
    status: Literal['success', 'failed', 'unrecorded', 'running', 'paused']
    checked_at: datetime | None = None
    success_at: datetime | None = None
    error_code: str | None = None
    reason: str = ''
    scope: dict = field(default_factory=dict)
    references: tuple[dict, ...] = ()

    def __post_init__(self):
        if self.status not in ('success', 'failed', 'unrecorded', 'running', 'paused'):
            raise ValueError('无效执行状态')
        for timestamp in (self.checked_at, self.success_at):
            if timestamp is not None and timestamp.utcoffset() is None:
                raise ValueError('执行时间缺少时区')
        if self.checked_at and self.success_at and self.success_at > self.checked_at:
            raise ValueError('最近成功时间晚于检查时间')
        if self.status == 'success' and not (self.checked_at and self.success_at):
            raise ValueError('成功凭证缺少时间')
        if self.status == 'failed' and not (self.checked_at and self.error_code):
            raise ValueError('失败凭证缺少检查时间或错误码')


@dataclass(frozen=True)
class EvidenceRecord:
    attempt: Attempt
    evidence: Evidence


@dataclass(frozen=True)
class Availability:
    kind: Literal['available','confirmed_empty','missing','unknown','not_applicable']
    period: str | None = None
    reason: str = ''
    source: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class Assessment:
    status: Literal['normal','failed','missing','excluded']
    reason: str


@dataclass(frozen=True)
class Binding:
    entry_id: str
    item_id: str
    subject_kind: str
    source_adapter: str
    availability_adapter: str
    dependencies: tuple[str, ...]
    writers: tuple[str, ...]
    unavailable_reason: str | None = None
    name: str = ""


@dataclass(frozen=True)
class AuditContext:
    view: Literal['stored','published'] = 'stored'
    product: str | None = None
    version: str | None = None
    period: str | None = None


@dataclass(frozen=True)
class StatusQuery:
    entry_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    subject_kind: str | None = None
    subject_keys: tuple[str, ...] = ()
    scope_kind: str = 'routine'
    context: AuditContext = field(default_factory=AuditContext)
    limit: int = 25
    cursor: str | None = None


@dataclass(frozen=True)
class StatusItem:
    key: StateKey
    name: str
    context: AuditContext
    evidence: Evidence
    availability: Availability
    assessment: Assessment


@dataclass(frozen=True)
class StatusPage:
    schema_version: str
    read_at: datetime
    items: list[StatusItem]
    next_cursor: str | None


def utc(value: datetime | None):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
