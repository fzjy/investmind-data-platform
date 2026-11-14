from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError


_FIXTURE_NAME = re.compile(r"^[a-z0-9-]+$")


class FixtureStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    @classmethod
    def default(cls) -> "FixtureStore":
        return cls(Path(__file__).resolve().parents[1] / "fixtures")

    def read(self, name: str) -> ProviderSnapshot[dict[str, object]]:
        if not _FIXTURE_NAME.fullmatch(name):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约")

        try:
            document = json.loads((self._root / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约") from exc

        if not isinstance(document, dict):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约")
        raw_data = document.get("data")
        raw_meta = document.get("meta")
        if not isinstance(raw_data, dict) or not isinstance(raw_meta, dict):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约")

        try:
            meta = DataMeta.model_validate(raw_meta)
        except ValueError as exc:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "数据源响应不符合契约") from exc

        return ProviderSnapshot(raw=cast(dict[str, object], raw_data), meta=meta)
