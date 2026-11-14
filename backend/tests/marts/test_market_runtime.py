"""市场离线重建：仅读取已入库证券数据，不采集、不触碰非目标产品。"""
import copy
import importlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Classification, ClassificationMember, DailyPrice, PublishedSnapshot
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key
from app.marts.publication import SnapshotPublisher
from app.stocks.universes.models import UniverseSelection
from backend.tests.marts.test_content_runtime import prepare, state
from backend.tests.jobs.test_build_and_publish_integration import _factory

NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
SELECTION = UniverseSelection("representative-50", "controlled", "representative", ("600519.SH",), "controlled")


def test_frozen_pulse_target_blocks_mixed_day_publication():
    from datetime import date
    from app.market.marts.market_runtime import rebuild_market_marts
    factory = _factory()
    prepare(factory)
    before = state(factory)
    report = rebuild_market_marts(factory, SELECTION, checked_at=NOW, dry_run=False,
        expected_as_of=date(2026, 8, 23))
    assert report['code'] == 'MARKET_TARGET_CHANGED'
    assert state(factory) == before


def test_corrupt_tagged_status_cannot_publish_as_known():
    from app.db.models import DailyBasic
    factory = _factory()
    prepare(factory)
    with factory.begin() as session:
        basic = session.scalar(select(DailyBasic))
        basic.data = {**basic.data, 'limit_status': 2, '_limitStatusLineage': {'sourcePayloadId': 999}}
    assert rebuild(factory)['status'] == 'completed'
    with factory() as session:
        stats = session.get(PublishedSnapshot, 'market-overview').payload['limitStats']
        assert stats['riseLimit'] is None and stats['coverage']['valid'] == 0


def test_pulse_then_ordinary_build_keeps_valid_limits_and_rejects_corruption():
    from app.db.models import DailyBasic, SourcePayload
    from app.market.pulse.repository import PulseRepository, FIELDS
    from app.connectors.tushare.contracts import TushareResponse
    from app.jobs.build_and_publish import BuildAndPublishOptions
    from backend.tests.jobs.test_build_and_publish_integration import _real_build_and_publish_job
    factory = _factory()
    prepare(factory)
    with factory() as session:
        basic = session.scalar(select(DailyBasic))
        code, day = basic.ts_code, basic.trade_date
    repo = PulseRepository(factory)
    assert repo.persist(code, day, 'controlled', TushareResponse(fields=FIELDS,
        items=((code, day.strftime('%Y%m%d'), 2),)), NOW)
    assert rebuild(factory)['status'] == 'completed'
    result = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(day, (code,)))
    assert result.status == 'completed'
    with factory() as session:
        stats = session.get(PublishedSnapshot, 'market-overview').payload['limitStats']
        assert stats['riseLimit'] == 1 and stats['coverage']['valid'] == 1
    with factory.begin() as session:
        session.scalar(select(SourcePayload)).content_hash = 'corrupt'
    assert _real_build_and_publish_job(factory).run(BuildAndPublishOptions(day, (code,))).status == 'completed'
    with factory() as session:
        assert session.get(PublishedSnapshot, 'market-overview').payload['limitStats']['coverage']['valid'] == 0


def test_pulse_publication_retains_non_target_missing_faults():
    factory = _factory()
    prepare(factory)
    fault = {'source': 'news', 'reason': 'SOURCE_PERMISSION_REQUIRED', 'code': 'OLD_NEWS_FAULT'}
    with factory.begin() as session:
        for product in ('market-overview', 'market-explorer'):
            pointer = session.get(PublishedSnapshot, product)
            pointer.payload = {**pointer.payload, 'missingData': [*pointer.payload.get('missingData', []), fault]}
    assert rebuild(factory)['status'] == 'completed'
    with factory() as session:
        for product in ('market-overview', 'market-explorer'):
            assert fault in session.get(PublishedSnapshot, product).payload['missingData']


def rebuild(factory, *, dry_run=False):
    assert importlib.util.find_spec("app.market.marts.market_runtime") is not None, "market-only rebuild missing"
    module = importlib.import_module("app.market.marts.market_runtime")
    return module.rebuild_market_marts(factory, SELECTION, checked_at=NOW, dry_run=dry_run)


def test_dry_run_does_not_write_and_reports_actual_scope():
    factory = _factory()
    prepare(factory)
    before = state(factory)
    report = rebuild(factory, dry_run=True)
    assert report["status"] == "planned" and report["requestCount"] == 0
    assert report["coverage"]["expected"] == 1 and report["coverage"]["prices"] == 1
    assert state(factory) == before


def test_only_market_pair_publishes_and_same_input_reentry_writes_nothing():
    factory = _factory()
    prepare(factory)
    before = state(factory)
    report = rebuild(factory)
    assert report["status"] == "completed"
    assert set(report["promotedProducts"]) == {"market-overview", "market-explorer"}
    after = state(factory)
    for product, original in before["pointers"].items():
        if product not in {"market-overview", "market-explorer"}:
            assert after["pointers"][product] == original
    assert len(after["pages"]) == len(before["pages"]) + 2
    assert after["workbenches"] == before["workbenches"]
    assert rebuild(factory)["status"] == "no_change"
    assert state(factory) == after


def test_missing_quotes_never_publish_empty_success():
    factory = _factory()
    prepare(factory)
    with factory.begin() as session:
        session.query(DailyPrice).delete()
    before = state(factory)
    report = rebuild(factory)
    assert report["status"] == "blocked" and report["code"] == "MARKET_NO_PRICES"
    assert state(factory) == before


def test_newer_one_sided_pointer_blocks_entire_pair():
    factory = _factory()
    prepare(factory)
    with factory.begin() as session:
        session.get(PublishedSnapshot, "market-explorer").as_of = NOW
    before = state(factory)
    report = rebuild(factory)
    assert report["code"] == "MARKET_NEWER_PUBLICATION"
    assert state(factory) == before


def test_second_product_failure_rolls_back_pair_and_history(monkeypatch):
    factory = _factory()
    prepare(factory)
    before = state(factory)
    original = SnapshotPublisher._write_page_snapshot
    def fail(self, session, candidate, version):
        if candidate.product == "market-explorer":
            raise RuntimeError("controlled failure")
        return original(self, session, candidate, version)
    monkeypatch.setattr(SnapshotPublisher, "_write_page_snapshot", fail)
    with pytest.raises(RuntimeError, match="controlled failure"):
        rebuild(factory)
    assert state(factory) == before


def test_publication_lock_is_shared_and_missing_calendar_is_not_fresh():
    factory = _factory()
    prepare(factory)
    lock = ProcessLocalJobLock()
    key = data_publication_lock_key()
    assert lock.try_acquire(key)
    try:
        before = state(factory)
        assert rebuild(factory)["code"] == "MARKET_LOCK_BUSY"
        assert state(factory) == before
    finally:
        lock.release(key)
    assert rebuild(factory)["status"] == "completed"
    with factory() as session:
        pointer = session.get(PublishedSnapshot, "market-overview")
        assert pointer.stale is True and pointer.stale_reason == "MARKET_CALENDAR_UNKNOWN"


def test_stock_only_rebuild_preserves_external_modules_and_news():
    factory = _factory()
    prepare(factory)
    news = [{"id": "existing", "title": "合法原记录", "time": "08:00", "source": "controlled", "publishedAt": NOW.isoformat()}]
    with factory.begin() as session:
        pointer = session.get(PublishedSnapshot, "market-explorer")
        payload = copy.deepcopy(pointer.payload)
        payload["marketNews"] = news
        pointer.payload = payload
    assert rebuild(factory)["status"] == "completed"
    with factory() as session:
        assert session.scalar(select(PublishedSnapshot).where(PublishedSnapshot.product == "market-explorer")).payload["marketNews"] == news


def test_null_prices_cannot_clear_good_rankings_and_claim_full_coverage():
    factory = _factory()
    prepare(factory)
    with factory.begin() as session:
        row = session.scalar(select(DailyPrice))
        row.close = None
        row.pct_chg = None
    before = state(factory)
    report = rebuild(factory)
    assert report["status"] == "blocked" and report["code"] == "MARKET_VALUES_UNAVAILABLE"
    assert state(factory) == before


def test_lost_membership_cannot_clear_existing_industry_heatmap():
    factory = _factory()
    prepare(factory)
    with factory.begin() as session:
        session.add(Classification(id=1, provider="tushare", source="SW2021", classification_id="root", name="消费", level=1))
        session.add(ClassificationMember(classification_id=1, ts_code="600519.SH", effective_from=NOW.date().replace(year=2020)))
    assert rebuild(factory)["status"] == "completed"
    with factory.begin() as session:
        session.query(ClassificationMember).delete()
    before = state(factory)
    report = rebuild(factory)
    assert report["status"] == "blocked" and report["code"] == "MARKET_CLASSIFICATION_REGRESSION"
    assert state(factory) == before


def test_other_source_members_are_not_counted_as_target_classification_coverage():
    factory = _factory()
    prepare(factory)
    with factory.begin() as session:
        session.add(Classification(id=1, provider="tushare", source="SW2014", classification_id="old", name="旧分类", level=1))
        session.add(ClassificationMember(classification_id=1, ts_code="600519.SH", effective_from=NOW.date().replace(year=2020)))
    report = rebuild(factory, dry_run=True)
    assert report["coverage"]["classified"] == 0
