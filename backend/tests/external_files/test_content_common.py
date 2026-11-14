"""内容公共能力：预期值来自手工证券目录，不从被测解析器推导。"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import ContentDocument, ExternalRecord, Security, SecurityNameHistory
from app.connectors.external_files.reader import ImportValidationFailed
from app.content.marts.content import ContentMartBuilder, ContentRepository
from backend.tests.external_files.test_raw_import import _importer, _content


def write(root, name="content.json", **changes):
    row = {**_content(), "externalId": "one", "summary": "摘要", "body": "正文", **changes}
    path = root / name
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("hints,want", [
    ({"securities": ["600000"]}, ["600000.SH"]),
    ({"securities": [], "securityNames": ["浦发银行"]}, ["600000.SH"]),
    ({"securities": [], "securityNames": ["浦发旧名"]}, ["600000.SH"]),
    ({"securities": [], "securityNames": ["上海浦东发展银行股份有限公司"]}, ["600000.SH"]),
    ({"securities": [], "securityNames": ["浦发"]}, []),
    ({"securities": [], "title": "浦发银行 600000.SH 宣布分红"}, []),
])
def test_exact_unique_hints_only(tmp_path, hints, want):
    root, importer, factory = _importer(tmp_path)
    with factory.begin() as session:
        session.get(Security, "600000.SH").data = {"fullname": "上海浦东发展银行股份有限公司"}
        session.add(SecurityNameHistory(ts_code="600000.SH", name="浦发旧名"))
    importer.import_file(write(root, **hints))
    with factory() as session:
        assert session.scalar(select(ContentDocument)).securities == want


def test_ambiguous_name_and_code_are_not_linked(tmp_path):
    root, importer, factory = _importer(tmp_path)
    with factory.begin() as session:
        session.add(Security(ts_code="600000.SZ", symbol="600000", name="浦发银行"))
    importer.import_file(write(root, securities=["600000"], securityNames=["浦发银行"]))
    with factory() as session:
        assert session.scalar(select(ContentDocument)).securities == []


def test_unknown_explicit_code_rejected_on_every_attempt(tmp_path):
    root, importer, factory = _importer(tmp_path)
    path = write(root, securities=["999999.SH"])
    for _ in range(2):
        with pytest.raises(ImportValidationFailed):
            importer.import_file(path)
    with factory() as session:
        assert session.scalar(select(ContentDocument)) is None


@pytest.mark.parametrize("status", ["link_only", "unknown", "restricted"])
def test_restricted_text_never_stored_or_displayed(tmp_path, status):
    root, importer, factory = _importer(tmp_path)
    importer.import_file(write(root, copyrightStatus=status))
    with factory() as session:
        document = session.scalar(select(ContentDocument))
        assert document.body is None and document.summary is None
        raw = session.scalar(select(ExternalRecord)).payload
        assert raw["body"] is None and raw["summary"] is None
    items = ContentMartBuilder(ContentRepository(factory)).build(datetime(2026, 10, 11, tzinfo=timezone.utc))
    if status == "link_only":
        assert len(items) == 1
        assert "summary" not in items[0] and "body" not in items[0]
        assert items[0]["id"] and items[0]["time"] and items[0]["market"]
    else:
        assert items == []


@pytest.mark.parametrize("status", ["licensed", "public_domain"])
def test_permitted_text_reaches_mart(tmp_path, status):
    root, importer, factory = _importer(tmp_path)
    importer.import_file(write(root, copyrightStatus=status))
    item = ContentMartBuilder(ContentRepository(factory)).build(datetime(2026, 10, 11, tzinfo=timezone.utc))[0]
    assert item["summary"] == "摘要" and item["body"] == "正文"


def test_content_duplicate_reports_content_products(tmp_path):
    root, importer, _ = _importer(tmp_path)
    path = write(root)
    first, second = importer.import_file(path), importer.import_file(path)
    assert first.affected_products == second.affected_products == ("market-explorer", "stock-workbench:600000.SH")


def test_linked_query_filters_before_limit():
    from backend.tests.jobs.test_build_and_publish_integration import _factory
    factory = _factory()
    with factory.begin() as session:
        for index in range(170):
            session.add(ContentDocument(source="x", dedupe_key=str(index), content_type="news", title=str(index),
                published_at=datetime(2026, 8, 16 if index == 0 else 17, tzinfo=timezone.utc), source_url="https://example.com",
                securities=["600000.SH"] if index == 0 else [], copyright_status="public_domain"))
    items = ContentMartBuilder(ContentRepository(factory)).build(datetime(2026, 10, 11, tzinfo=timezone.utc), limit=1, ts_code="600000.SH")
    assert [item["title"] for item in items] == ["0"]


def test_content_cutoff_compares_the_same_instant_across_timezones(tmp_path):
    root, importer, factory = _importer(tmp_path)
    importer.import_file(write(root, publishedAt="2026-08-17T10:00:00+08:00", copyrightStatus="licensed"))
    builder = ContentMartBuilder(ContentRepository(factory))
    assert builder.build(datetime(2026, 8, 17, 1, tzinfo=timezone.utc)) == []
    assert [item["title"] for item in builder.build(datetime(2026, 8, 17, 3, tzinfo=timezone.utc))] == ["测试新闻"]


def test_mixed_file_security_errors_keep_original_record_numbers(tmp_path):
    root, importer, _ = _importer(tmp_path)
    path = root / "mixed.jsonl"
    macro = {"schemaVersion": "1.0", "recordType": "macro", "source": "controlled",
        "seriesId": "cn.cpi.yoy", "name": "CPI", "region": "CN", "frequency": "monthly",
        "unit": "%", "seasonalAdjustment": "not_adjusted", "observedAt": "2026-08-01",
        "releasedAt": "2026-09-09T00:00:00Z", "revision": "one", "value": "1",
        "sourceUrl": "https://example.com/macro"}
    path.write_text("\n".join(json.dumps(row) for row in (
        macro, {**_content(), "securities": ["999999.SH"]})), encoding="utf-8")
    with pytest.raises(ImportValidationFailed) as caught:
        importer.import_file(path)
    assert caught.value.errors[0]["record"] == 2
