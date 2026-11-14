from pathlib import Path

import pytest

from app.connectors.external_files.reader import ExternalFileReader, ImportPathError


def test_reader_rejects_path_outside_import_root(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    outside = tmp_path / "escape.jsonl"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ImportPathError):
        list(ExternalFileReader(root).read(outside))
