"""仅隔离运行器登记的一次性 PostgreSQL17；无业务库/外网。"""
import os
import json
from datetime import date

import pytest
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.connectors.external_files.reader import ExternalFileReader
from app.db.base import Base, DerivedBase, ExternalBase, MartBase
from app.db.models import ContentDocument, PublishedSnapshot
from app.db.session import create_engine_and_session
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.content.marts.content_runtime import rebuild_content_marts
from app.ingestion.external_import import ExternalImporter
from app.marts.publication import SnapshotPublisher
from app.shared.repository import SnapshotRepository
from sqlalchemy import select
from backend.tests.marts.test_content_runtime import NOW, PRODUCT, SYMBOL, add_content, state
from backend.tests.jobs.test_build_and_publish_integration import _factory, _seed_complete_core_sample, _real_build_and_publish_job

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1", reason="requires registered disposable PostgreSQL17")]


@pytest.fixture
def database():
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        engine, factory = create_engine_and_session(url)
        with engine.begin() as connection:
            for schema in ("core", "raw", "ops", "derived", "mart"):
                connection.execute(text(f"CREATE SCHEMA {schema}"))
            assert int(connection.scalar(text("SHOW server_version_num"))) // 10000 == 17
        for base in (Base, DerivedBase, ExternalBase, MartBase):
            base.metadata.create_all(engine)
        try:
            # Existing SQLite seed helper does not flush security before dependents.
            # Copy its controlled core rows in FK order, then run the real builders.
            seed_factory = _factory()
            _seed_complete_core_sample(seed_factory)
            with seed_factory.kw["bind"].connect() as source, engine.begin() as target:
                for table in Base.metadata.sorted_tables:
                    records = [dict(row) for row in source.execute(select(table)).mappings()]
                    if records:
                        target.execute(table.insert(), records)
            seed_factory.kw["bind"].dispose()
            result = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(date(2026, 8, 24), (SYMBOL,)))
            assert result.status == "completed"
            add_content(factory)
            yield Settings(database_url=url), factory, engine
        finally:
            engine.dispose()


def test_pg_real_entry_publishes_then_same_value_changes_nothing(database):
    settings, factory, _ = database
    before = state(factory)
    report = rebuild_content_marts(settings, as_of=NOW)
    assert report["status"] == "completed" and report["promotedProducts"] == ["market-explorer", PRODUCT]
    after = state(factory)
    assert len(after["pages"]) == len(before["pages"]) + 2
    assert SnapshotRepository(factory).read(PRODUCT).raw["dailyChart"]["announcements"][0]["label"] == "年度报告"
    assert rebuild_content_marts(settings, as_of=NOW)["promotedProducts"] == []
    assert state(factory) == after


def test_pg_failure_rolls_back_current_history_and_rights_redaction(database, monkeypatch):
    settings, factory, _ = database
    assert rebuild_content_marts(settings, as_of=NOW)["status"] == "completed"
    with factory.begin() as session:
        session.scalar(select(ContentDocument)).copyright_status = "link_only"
    before = state(factory)
    original = SnapshotPublisher._write_page_snapshot
    def fail(self, session, candidate, version):
        if candidate.product == PRODUCT:
            raise RuntimeError("controlled interruption")
        original(self, session, candidate, version)
    monkeypatch.setattr(SnapshotPublisher, "_write_page_snapshot", fail)
    assert rebuild_content_marts(settings, as_of=NOW)["status"] == "failed"
    assert state(factory) == before
    monkeypatch.setattr(SnapshotPublisher, "_write_page_snapshot", original)
    assert rebuild_content_marts(settings, as_of=NOW)["status"] == "completed"
    assert "许可正文" not in str(state(factory)) and "许可摘要" not in str(state(factory))


def test_pg_shared_advisory_lock_prevents_external_publication(database):
    settings, factory, engine = database
    lock = PostgresAdvisoryJobLock(engine)
    before = state(factory)
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert rebuild_content_marts(settings, as_of=NOW)["code"] == "CONTENT_LOCK_BUSY"
        assert state(factory) == before
    finally:
        lock.release(data_publication_lock_key())
    assert rebuild_content_marts(settings, as_of=NOW)["status"] == "completed"


def test_pg_file_import_resolves_exact_name_and_applies_link_only_policy(database, tmp_path):
    settings, factory, _ = database
    path = tmp_path / "content.json"
    path.write_text(json.dumps({"schemaVersion": "1.0", "recordType": "content", "source": "controlled-file",
        "externalId": "one", "contentType": "news", "title": "关联新闻", "summary": "不得保存摘要", "body": "不得保存正文",
        "publishedAt": "2026-08-25T19:00:00+08:00", "sourceUrl": "https://example.com/news",
        "securityNames": ["贵州茅台"], "copyrightStatus": "link_only"}, ensure_ascii=False), encoding="utf-8")
    importer = ExternalImporter(factory, ExternalFileReader(tmp_path))
    first, second = importer.import_file(path), importer.import_file(path)
    assert first.affected_products == second.affected_products == ("market-explorer", PRODUCT)
    with factory() as session:
        row = session.scalar(select(ContentDocument).where(ContentDocument.source == "controlled-file"))
        assert row.securities == [SYMBOL] and row.summary is None and row.body is None
        assert row.published_at.isoformat() == "2026-08-25T11:00:00+00:00"
    assert rebuild_content_marts(settings, as_of=NOW)["status"] == "completed"
    news = SnapshotRepository(factory).read("market-explorer").raw["marketNews"]
    imported = next(item for item in news if item["source"] == "controlled-file")
    assert "summary" not in imported and "body" not in imported


def test_pg_revocation_scrubs_history_without_any_current_pointer(database):
    settings, factory, _ = database
    assert rebuild_content_marts(settings, as_of=NOW)["status"] == "completed"
    with factory.begin() as session:
        version = session.get(PublishedSnapshot, PRODUCT).version
        session.scalar(select(ContentDocument)).copyright_status = "link_only"
        for row in session.scalars(select(PublishedSnapshot)):
            session.delete(row)
    before = state(factory)
    report = rebuild_content_marts(settings, as_of=NOW)
    assert report["status"] == "completed" and report["code"] == "CONTENT_HISTORY_REDACTED"
    assert report["promotedProducts"] == [] and state(factory)["pointers"] == {}
    history = SnapshotRepository(factory).read_versioned(PRODUCT, version).raw
    assert history["dailyChart"]["announcements"][0]["sourceUrl"] == "https://example.com/notice"
    assert "许可正文" not in str(state(factory)) and "许可摘要" not in str(state(factory))
    assert len(state(factory)["pages"]) == len(before["pages"])
