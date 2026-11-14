"""发布测试以 core 数据经原有 builder 生成基础快照，禁止 fixture 回退。"""
import copy
import importlib
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import ContentDocument, PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key
from app.marts.publication import SnapshotPublisher
from app.market.database_provider import MarketDatabaseProvider
from app.stocks.database_provider import StocksDatabaseProvider
from app.shared.repository import SnapshotRepository
from backend.tests.jobs.test_build_and_publish_integration import _factory, _seed_complete_core_sample, _real_build_and_publish_job

NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
SYMBOL = "600519.SH"
PRODUCT = "stock-workbench:" + SYMBOL


def runtime():
    assert importlib.util.find_spec("app.content.marts.content_runtime") is not None, "content runtime missing"
    return importlib.import_module("app.content.marts.content_runtime")


def add_content(factory, *, status="licensed", kind="announcement", linked=True):
    with factory.begin() as session:
        session.add(ContentDocument(source="controlled", dedupe_key="notice", content_type=kind,
            title="年度报告", summary="许可摘要", body="许可正文", published_at=NOW - timedelta(hours=1),
            source_url="https://example.com/notice", securities=[SYMBOL] if linked else [], copyright_status=status))


def prepare(factory):
    _seed_complete_core_sample(factory)
    result = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(date(2026, 8, 24), (SYMBOL,)))
    assert result.status == "completed"


def state(factory):
    with factory() as session:
        return {
            "pointers": {p.product: (p.version, p.as_of, copy.deepcopy(p.payload), p.stale, p.stale_reason, p.published_at, p.freshness_checked_at)
                         for p in session.scalars(select(PublishedSnapshot))},
            "pages": [(p.product, p.snapshot_version, copy.deepcopy(p.payload)) for p in session.scalars(select(PageSnapshot).order_by(PageSnapshot.id))],
            "workbenches": [(p.snapshot_version, copy.deepcopy(p.payload)) for p in session.scalars(select(StockWorkbenchSnapshot).order_by(StockWorkbenchSnapshot.id))],
        }


def run(factory, monkeypatch, *, as_of=NOW):
    module = runtime()
    # The in-memory engine belongs to this test, not to the runtime adapter.
    monkeypatch.setattr(factory.kw["bind"], "dispose", lambda: None)
    monkeypatch.setattr(module, "create_engine_and_session", lambda _url: (factory.kw["bind"], factory))
    return module.rebuild_content_marts(Settings(database_url="sqlite:///controlled"), as_of=as_of)


def test_no_content_leaves_every_snapshot_unchanged(monkeypatch):
    factory = _factory()
    prepare(factory)
    before = state(factory)
    report = run(factory, monkeypatch)
    assert report["status"] == "no_change" and report["code"] == "CONTENT_NO_DOCUMENTS"
    assert state(factory) == before


def test_stock_rebuild_keeps_each_visible_document_at_snapshot_cutoff(monkeypatch):
    factory = _factory()
    prepare(factory)
    disclosed = datetime(2026, 8, 24, 7, tzinfo=timezone.utc)
    with factory.begin() as session:
        for key, status, stamp in [
            ("annual", "licensed", disclosed), ("summary", "link_only", disclosed),
            ("withdrawn", "blocked", disclosed), ("tomorrow", "licensed", disclosed + timedelta(days=1)),
        ]:
            session.add(ContentDocument(source="controlled", dedupe_key=key, content_type="announcement",
                title=key, body="正文", published_at=stamp, source_url=f"https://example.com/{key}",
                securities=[SYMBOL], copyright_status=status))
    run(factory, monkeypatch, as_of=disclosed)
    def chart_and_mentions():
        payload = state(factory)["pointers"][PRODUCT][2]
        return payload["dailyChart"]["announcements"], payload["quote"]["mentions"], payload["quote"]["latestMention"]
    expected = chart_and_mentions()
    assert [item["label"] for item in expected[0]] == ["summary", "annual"]
    assert "body" not in expected[0][0] and expected[1] == 2
    assert all(item["placementStatus"] == "pending" and item["disclosureTime"] == "15:00"
               for item in expected[0])
    prepare_job = _real_build_and_publish_job(factory)
    report = prepare_job.run(BuildAndPublishOptions(disclosed.date(), (SYMBOL,)))
    assert report.status == "completed"
    assert chart_and_mentions() == expected


def test_no_base_never_builds_sample_snapshots(monkeypatch):
    factory = _factory()
    add_content(factory)
    report = run(factory, monkeypatch)
    assert report["status"] == "blocked" and report["code"] == "CONTENT_NO_BASE_SNAPSHOT"
    assert state(factory)["pointers"] == {} and state(factory)["pages"] == []


def test_shanghai_midnight_notice_publishes_pending_until_next_candle(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    stamp = datetime(2026, 8, 24, 16, tzinfo=timezone.utc)
    with factory.begin() as session:
        session.scalar(select(ContentDocument)).published_at = stamp
    assert run(factory, monkeypatch, as_of=stamp)["status"] == "completed"
    item, = state(factory)["pointers"][PRODUCT][2]["dailyChart"]["announcements"]
    assert (item["disclosedOn"], item["disclosureTime"], item["placementStatus"]) == ("2026-08-25", None, "pending")


def test_real_publication_preserves_other_fields_and_is_idempotent(monkeypatch):
    factory = _factory()
    prepare(factory)
    before = state(factory)
    add_content(factory)
    report = run(factory, monkeypatch)
    assert report["status"] == "completed"
    assert report["promotedProducts"] == ["market-explorer", PRODUCT]
    repository = SnapshotRepository(factory)
    market = MarketDatabaseProvider(repository).read_explorer()
    workbench = StocksDatabaseProvider(repository, factory).read_workbench(SYMBOL)
    assert market.raw["marketNews"][0]["title"] == "年度报告"
    assert workbench.raw["dailyChart"]["announcements"][0]["label"] == "年度报告"
    assert workbench.raw["dailyChart"]["announcements"][0]["kind"] == "other"
    after = state(factory)
    for product in before["pointers"]:
        previous, current = before["pointers"][product], after["pointers"][product]
        if product not in {"market-explorer", PRODUCT}:
            assert current == previous
            continue
        assert current[1] == previous[1] and current[3:5] == previous[3:5]
        old_payload, new_payload = copy.deepcopy(previous[2]), copy.deepcopy(current[2])
        if product == "market-explorer":
            old_payload.pop("marketNews")
            new_payload.pop("marketNews")
        else:
            for payload in (old_payload, new_payload):
                payload["dailyChart"].pop("announcements")
                payload["quote"].pop("mentions")
                payload["quote"].pop("latestMention")
        assert new_payload == old_payload
    assert len(after["pages"]) == len(before["pages"]) + 2
    assert len(after["workbenches"]) == len(before["workbenches"]) + 1
    again = run(factory, monkeypatch, as_of=NOW + timedelta(hours=1))
    assert again["status"] == "completed" and again["promotedProducts"] == []
    assert state(factory) == after


def test_failed_second_product_rolls_back_first_product_and_history(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    before = state(factory)
    original = SnapshotPublisher._write_page_snapshot
    def fail(self, session, candidate, version):
        if candidate.product == PRODUCT:
            raise RuntimeError("postgresql://secret-user:secret-pass@private-host/database")
        return original(self, session, candidate, version)
    monkeypatch.setattr(SnapshotPublisher, "_write_page_snapshot", fail)
    report = run(factory, monkeypatch)
    assert report["status"] == "failed" and report["code"] == "CONTENT_REBUILD_FAILED"
    assert "secret" not in str(report) and "private-host" not in str(report)
    assert state(factory) == before


def test_bad_base_quality_blocks_all_products(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    with factory.begin() as session:
        row = session.get(PublishedSnapshot, PRODUCT)
        payload = copy.deepcopy(row.payload)
        payload["quote"]["price"] = None
        row.payload = payload
    before = state(factory)
    report = run(factory, monkeypatch)
    assert report["status"] == "blocked" and report["code"] == "CONTENT_QUALITY_BLOCKED"
    assert state(factory) == before


def test_shared_publication_lock_blocks_rebuild(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    before = state(factory)
    lock = ProcessLocalJobLock()
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert run(factory, monkeypatch)["code"] == "CONTENT_LOCK_BUSY"
    finally:
        lock.release(data_publication_lock_key())
    assert state(factory) == before


@pytest.mark.parametrize("status", ["unknown", "restricted", "link_only"])
def test_rights_downgrade_scrubs_current_and_versioned_snapshots(monkeypatch, status):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    assert run(factory, monkeypatch)["status"] == "completed"
    with factory.begin() as session:
        session.scalar(select(ContentDocument)).copyright_status = status
    report = run(factory, monkeypatch)
    assert report["status"] == "completed"
    snapshots = state(factory)
    serialized = str(snapshots)
    assert "许可正文" not in serialized and "许可摘要" not in serialized
    rows = snapshots["pointers"]["market-explorer"][2]["marketNews"]
    assert len(rows) == (1 if status == "link_only" else 0)


def test_missing_configuration_is_safe():
    report = runtime().rebuild_content_marts(Settings(database_url=None, database_read_url=None))
    assert report["status"] == "blocked" and report["code"] == "CONTENT_DATABASE_REQUIRED"


def test_relicensing_can_publish_after_historical_redaction(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    assert run(factory, monkeypatch)["status"] == "completed"
    for status in ("link_only", "licensed"):
        with factory.begin() as session:
            session.scalar(select(ContentDocument)).copyright_status = status
        report = run(factory, monkeypatch)
        assert report["status"] == "completed"
    assert SnapshotRepository(factory).read("market-explorer").raw["marketNews"][0]["body"] == "许可正文"


def test_legacy_numeric_id_cannot_borrow_another_documents_license(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    with factory.begin() as session:
        for row in session.scalars(select(PageSnapshot).where(PageSnapshot.product == "market-explorer")):
            payload = copy.deepcopy(row.payload)
            payload["marketNews"] = [{"id": "1", "source": "unverified", "title": "other", "body": "legacy secret"}]
            row.payload = payload
    assert run(factory, monkeypatch)["status"] == "completed"
    assert "legacy secret" not in str(state(factory))


def test_default_time_uses_real_clock_and_configured_database(tmp_path):
    from app.db.session import create_engine_and_session
    from app.db.base import Base, DerivedBase, ExternalBase, MartBase
    url = "sqlite:///" + (tmp_path / "content.db").as_posix()
    engine, factory = create_engine_and_session(url)
    for base in (Base, DerivedBase, ExternalBase, MartBase):
        base.metadata.create_all(engine)
    prepare(factory)
    add_content(factory)
    before = datetime.now(timezone.utc)
    report = runtime().rebuild_content_marts(Settings(database_url=url))
    after = datetime.now(timezone.utc)
    assert report["status"] == "completed"
    assert before <= datetime.fromisoformat(report["asOf"]) <= after
    assert SnapshotRepository(factory).read("market-explorer").raw["marketNews"][0]["title"] == "年度报告"
    engine.dispose()


def test_link_only_history_keeps_metadata_only(monkeypatch):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    assert run(factory, monkeypatch)["status"] == "completed"
    with factory.begin() as session:
        document = session.scalar(select(ContentDocument))
        document.copyright_status = "link_only"
        for row in session.scalars(select(PageSnapshot).where(PageSnapshot.product == "market-explorer")):
            if row.payload["marketNews"]:
                payload = copy.deepcopy(row.payload)
                payload["marketNews"][0]["fullText"] = "旧版全文"
                row.payload = payload
    assert run(factory, monkeypatch)["status"] == "completed"
    assert "旧版全文" not in str(state(factory))


@pytest.mark.parametrize("status", ["link_only", "unknown", "restricted"])
@pytest.mark.parametrize("remove_all_bases", [False, True])
def test_revoked_history_without_current_pointer_is_redacted(monkeypatch, status, remove_all_bases):
    factory = _factory()
    prepare(factory)
    add_content(factory)
    assert run(factory, monkeypatch)["status"] == "completed"
    with factory.begin() as session:
        version = session.get(PublishedSnapshot, PRODUCT).version
        session.scalar(select(ContentDocument)).copyright_status = status
        for row in session.scalars(select(PublishedSnapshot)):
            if remove_all_bases or row.product == PRODUCT:
                session.delete(row)
    before = state(factory)
    report = run(factory, monkeypatch)
    assert report["status"] == "completed", report
    assert report["redactedHistoryCount"] > 0
    assert PRODUCT not in report["promotedProducts"]
    repository = SnapshotRepository(factory)
    versioned = repository.read_versioned(PRODUCT, version).raw
    fallback = StocksDatabaseProvider(repository, factory).read_workbench(SYMBOL).raw
    assert len(versioned["dailyChart"]["announcements"]) == (1 if status == "link_only" else 0)
    for payload in (versioned, fallback):
        assert "许可正文" not in str(payload) and "许可摘要" not in str(payload)
        items = payload["dailyChart"]["announcements"]
        # 原provider对相同行情时点的多版本回退可选择无公告的原版本。
        assert len(items) <= (1 if status == "link_only" else 0)
    after = state(factory)
    assert len(after["workbenches"]) == len(before["workbenches"])
    assert PRODUCT not in after["pointers"]
    if remove_all_bases:
        assert after["pointers"] == {} and report["promotedProducts"] == []
