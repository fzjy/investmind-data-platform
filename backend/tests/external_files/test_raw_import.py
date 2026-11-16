import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.connectors.external_files.reader import ExternalFileReader, ImportValidationFailed
from app.db.base import Base, ExternalBase
from app.db.models import ContentDocument, ExternalFile, Security
from app.ingestion.external_import import ExternalImporter


def _importer(tmp_path: Path, factory):
    root = tmp_path / "imports"
    root.mkdir()
    with factory.begin() as session:
        session.add(Security(ts_code="600000.SH", symbol="600000", name="浦发银行"))
    return root, ExternalImporter(factory, ExternalFileReader(root)), factory


def _content() -> dict[str, object]:
    return {"schemaVersion": "1.0", "recordType": "content", "source": "example", "contentType": "news", "title": "测试新闻", "publishedAt": "2026-08-17T10:00:00+08:00", "sourceUrl": "https://example.com/news/1", "securities": ["600000.SH"], "copyrightStatus": "link_only"}


def test_importing_same_file_twice_is_idempotent(pg_factory, tmp_path: Path) -> None:
    root, importer, factory = _importer(tmp_path, pg_factory)
    path = root / "content.jsonl"
    path.write_text(json.dumps(_content(), ensure_ascii=False), encoding="utf-8")
    first = importer.import_file(path)
    second = importer.import_file(path)
    assert second.status == "already_imported"
    assert second.file_id == first.file_id
    with factory() as session:
        assert session.scalar(select(ContentDocument).count()) if False else session.query(ContentDocument).count() == 1


def test_invalid_line_prevents_core_write(pg_factory, tmp_path: Path) -> None:
    root, importer, factory = _importer(tmp_path, pg_factory)
    path = root / "bad.jsonl"
    path.write_text(json.dumps(_content()) + "\n{\"recordType\":\"content\"}\n", encoding="utf-8")
    with pytest.raises(ImportValidationFailed):
        importer.import_file(path)
    with factory() as session:
        assert session.query(ContentDocument).count() == 0
        assert session.query(ExternalFile).count() == 1
