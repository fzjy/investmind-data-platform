from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.connectors.external_files.contracts import ContentImportRecord, ImportRecord, MacroImportRecord


class ImportPathError(ValueError):
    pass


class ImportValidationFailed(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"外部数据校验失败: {len(errors)} 条")
        self.errors = errors


class ExternalFileReader:
    def __init__(self, import_root: Path, *, max_line_bytes: int = 2_000_000) -> None:
        self.import_root = import_root.resolve()
        self.max_line_bytes = max_line_bytes

    def _safe_path(self, path: Path) -> Path:
        candidate = path.resolve()
        if not candidate.is_relative_to(self.import_root):
            raise ImportPathError("文件必须位于配置的导入目录内")
        if candidate.suffix.lower() not in {".json", ".jsonl"}:
            raise ImportPathError("仅支持 .json 或 .jsonl 文件")
        if not candidate.is_file():
            raise ImportPathError("导入文件不存在")
        return candidate

    def _models(self, raw: object, row_number: int) -> ImportRecord:
        try:
            if not isinstance(raw, dict):
                raise ValueError("记录必须是对象")
            record_type = raw.get("recordType")
            model = MacroImportRecord if record_type == "macro" else ContentImportRecord if record_type == "content" else None
            if model is None:
                raise ValueError("recordType 必须为 macro 或 content")
            return model.model_validate(raw)  # type: ignore[return-value]
        except (ValidationError, ValueError) as exc:
            detail = exc.errors() if isinstance(exc, ValidationError) else [{"message": str(exc)}]
            raise ImportValidationFailed([{"row": row_number, "errors": detail}]) from exc

    def read(self, path: Path) -> Iterator[ImportRecord]:
        safe_path = self._safe_path(path)
        try:
            if safe_path.stat().st_size == 0:
                raise ImportValidationFailed([{"row": 0, "errors": [{"message": "空文件"}]}])
            if safe_path.suffix.lower() == ".jsonl":
                with safe_path.open("r", encoding="utf-8") as stream:
                    found = False
                    for row_number, line in enumerate(stream, 1):
                        if len(line.encode("utf-8")) > self.max_line_bytes:
                            raise ImportValidationFailed([{"row": row_number, "errors": [{"message": "单行超过大小限制"}]}])
                        if not line.strip():
                            continue
                        found = True
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ImportValidationFailed([{"row": row_number, "errors": [{"message": "JSON 无效"}]}]) from exc
                        yield self._models(raw, row_number)
                    if not found:
                        raise ImportValidationFailed([{"row": 0, "errors": [{"message": "空文件"}]}])
            else:
                try:
                    raw = json.loads(safe_path.read_text(encoding="utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ImportValidationFailed([{"row": 1, "errors": [{"message": "JSON 无效或不是 UTF-8"}]}]) from exc
                records = raw if isinstance(raw, list) else [raw]
                if not records:
                    raise ImportValidationFailed([{"row": 0, "errors": [{"message": "空文件"}]}])
                for row_number, item in enumerate(records, 1):
                    yield self._models(item, row_number)
        except UnicodeError as exc:
            raise ImportValidationFailed([{"row": 0, "errors": [{"message": "文件不是 UTF-8"}]}]) from exc
