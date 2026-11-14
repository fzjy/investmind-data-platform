from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict


Scalar: TypeAlias = str | int | float | bool | None


class TushareResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    fields: tuple[str, ...]
    items: tuple[tuple[Scalar, ...], ...]

    def rows(self) -> tuple[dict[str, Scalar], ...]:
        rows: list[dict[str, Scalar]] = []
        for item in self.items:
            if len(item) != len(self.fields):
                raise ValueError("Tushare fields/items length mismatch")
            rows.append(dict(zip(self.fields, item, strict=True)))
        return tuple(rows)


def request_hash(api_name: str, params: Mapping[str, Scalar], fields: Sequence[str]) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {"api_name": api_name, "params": dict(params), "fields": list(fields)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
