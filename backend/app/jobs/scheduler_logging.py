from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TextIO
import sys


_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler log timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class SchedulerJsonLogger:
    def __init__(
        self,
        scheduler_instance_id: str | None,
        *,
        sanitize: Callable[[str], str] = lambda value: value,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        output: TextIO | None = None,
        min_level: str = "info",
    ) -> None:
        if min_level not in _LEVELS:
            raise ValueError("invalid scheduler log level")
        self._min_level = _LEVELS[min_level]
        self._scheduler_instance_id = scheduler_instance_id
        self._sanitize = sanitize
        self._clock = clock
        self._output = output

    def emit(self, event: str, level: str, **fields: object) -> None:
        if level not in _LEVELS:
            raise ValueError("invalid scheduler log level")
        if _LEVELS[level] < self._min_level:
            return
        payload: dict[str, object] = {
            "timestamp": _utc_timestamp(self._clock()),
            "level": level,
            "event": event,
            "schedulerInstanceId": self._scheduler_instance_id,
        }
        payload.update(
            {
                _camel_case(name): self._field_value(name, value)
                for name, value in fields.items()
                if value is not None
            }
        )
        print(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            file=self._output or sys.stdout,
            flush=True,
        )

    def _field_value(self, name: str, value: object) -> object:
        sanitized = self._sanitize_value(value)
        if name == "message" and isinstance(sanitized, str):
            return sanitized[:500]
        return sanitized

    def _sanitize_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._sanitize(value)
        if isinstance(value, datetime):
            return _utc_timestamp(value)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {
                str(key): self._sanitize_value(nested)
                for key, nested in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [self._sanitize_value(nested) for nested in value]
        return value
