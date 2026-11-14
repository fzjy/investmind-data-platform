from datetime import timedelta

from sqlalchemy import select
from smartparse_news import Config
from app.content.news_retention import cleanup_news
from app.content.news_ingestion import ingest_news
from app.content.news_storage import DatabaseNewsStore
from app.db.models import ContentDocument, ExternalFile, ExternalRecord, JobCursor
from backend.tests.marts.test_news_unified import NOW, RESPONSE
from backend.tests.jobs.test_build_and_publish_integration import _factory
from smartparse_news.collector import normalize


def seed(factory, days=366, pending=False):
    store = DatabaseNewsStore(factory)
    records = normalize(RESPONSE, NOW)
    for i, row in enumerate(records):
        row['publishedAt'] = (NOW-timedelta(days=days if i == 0 else 365)).isoformat()
    with store.transaction() as tx:
        for row in records: tx.append(row, NOW.isoformat())
    batch = store.pending()
    ingest_news(factory, batch)
    if not pending: store.ack([r['queueId'] for r in batch])
    return store


def test_retention_default_and_shared_raw_batch_boundary():
    assert Config().retention_days == 365
    factory = _factory(); store = seed(factory)
    prior = store.status()
    result = cleanup_news(factory, Config(), NOW)
    assert result['status'] == 'completed'
    with factory() as session:
        assert [r.external_id for r in session.scalars(select(ContentDocument))] == ['1']
        assert len(session.scalars(select(ExternalRecord)).all()) == 1
        assert len(session.scalars(select(ExternalFile)).all()) == 1
    assert store.status() == prior
    assert cleanup_news(factory, Config(), NOW)['status'] == 'not_due'
    cleanup_news(factory, Config(), NOW+timedelta(days=2))
    with factory() as session:
        assert session.scalar(select(ContentDocument)) is None
        assert session.scalar(select(ExternalRecord)) is None
        assert session.scalar(select(ExternalFile)) is None


def test_pending_records_remain_until_delivery_and_next_cycle():
    factory = _factory(); store = seed(factory, pending=True)
    cleanup_news(factory, Config(), NOW)
    with factory() as session:
        assert len(session.scalars(select(ContentDocument)).all()) == 2
        assert len(session.scalars(select(ExternalRecord)).all()) == 2
    assert len(store.pending()) == 2
    store.ack([r['queueId'] for r in store.pending()])
    cleanup_news(factory, Config(), NOW+timedelta(days=2))
    with factory() as session: assert session.scalar(select(ContentDocument)) is None


def test_cleanup_resumes_small_batches():
    factory = _factory(); seed(factory)
    config = Config(cleanup_batch_size=1, cleanup_max_batches=1)
    assert cleanup_news(factory, config, NOW)['status'] == 'running'
    for _ in range(40):
        report = cleanup_news(factory, config, NOW)
        if report['status'] == 'completed': break
    assert report['status'] == 'completed'
    with factory() as session: assert len(session.scalars(select(ContentDocument)).all()) == 1


def test_invalid_retention_settings_are_rejected():
    import pytest
    for values in ({'retention_days': 0}, {'cleanup_batch_size': -1}, {'log_retention_days': True}):
        with pytest.raises(ValueError): Config(**values)


def test_snapshot_cleanup_preserves_prices_dates_other_sources_and_pending_news():
    from copy import deepcopy
    from app.db.models import PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot
    from app.content.marts.content import ContentMartBuilder
    factory = _factory(); store = seed(factory)
    with factory.begin() as session:
        docs = session.scalars(select(ContentDocument).order_by(ContentDocument.id)).all()
        items = ContentMartBuilder.from_documents(docs)
        other = {**items[0], 'source': 'other-provider'}
        announcement = {**items[0], 'contentType': 'announcement'}
        payload = {'marketNews': [*items, other, announcement], 'prices': {'close': 123}, 'asOf': 'old-price-date'}
        session.add(PageSnapshot(product='market-explorer', snapshot_version='old', as_of=NOW, payload=deepcopy(payload)))
        session.add(PublishedSnapshot(product='market-explorer', version='old', as_of=NOW, stale=True, payload=deepcopy(payload)))
        session.add(StockWorkbenchSnapshot(ts_code='test', snapshot_version='old', as_of=NOW.date(), payload=deepcopy(payload)))
    cleanup_news(factory, Config(), NOW)
    with factory() as session:
        for model in (PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot):
            row = session.scalar(select(model))
            assert row.payload['marketNews'] == [items[1], other, announcement]
            assert row.payload['prices'] == {'close': 123} and row.payload['asOf'] == 'old-price-date'
        assert session.scalar(select(PublishedSnapshot)).version == 'old'


def test_failed_batch_rolls_back_and_alert_visible_then_recovers(monkeypatch):
    from app.content import news_retention as module
    from app.content.routes import read_news
    factory = _factory(); seed(factory)
    original = module._step
    def fail_after_delete(session, state, config):
        original(session, state, config)
        raise RuntimeError('controlled')
    monkeypatch.setattr(module, '_step', fail_after_delete)
    assert module.run_cleanup(factory, Config(), NOW)['status'] == 'failed'
    with factory() as session:
        assert len(session.scalars(select(ContentDocument)).all()) == 2
        assert session.scalar(select(JobCursor).where(JobCursor.cursor_key == 'retention')) is None
    assert read_news(factory, now=NOW)['health']['code'] == 'NEWS_RETENTION_FAILED'
    monkeypatch.setattr(module, '_step', original)
    assert module.run_cleanup(factory, Config(), NOW)['status'] == 'completed'
    assert read_news(factory, now=NOW)['health']['code'] is None


def test_events_active_faults_and_collection_cursor_are_protected():
    from app.content.news_runtime import save_health
    from app.db.models import OperationalAlert
    factory = _factory(); store = seed(factory)
    old = NOW-timedelta(days=100)
    with store.transaction() as tx:
        tx.event(old.isoformat(), 'SOURCE_FAILED')
        tx.event(NOW.isoformat(), 'RECENT_EVENT')
        state = tx.load(); state.update(nextId=1234, lastError='SOURCE_FAILED', failures=2)
        tx.save(state)
    save_health(factory, store.status(), Config(), old)
    cleanup_news(factory, Config(), NOW)
    with factory() as session:
        assert len(session.scalar(select(JobCursor).where(JobCursor.cursor_key == 'events')).cursor_value['items']) == 2
        assert session.scalar(select(OperationalAlert)).status == 'active'
    with store.transaction() as tx:
        state = tx.load(); state.update(lastError=None, failures=0, lastSuccessAt=old.isoformat()); tx.save(state)
    save_health(factory, store.status(), Config(), old)
    cleanup_news(factory, Config(), NOW+timedelta(days=1))
    assert store.status()['nextId'] == 1234
    with factory() as session:
        assert len(session.scalar(select(JobCursor).where(JobCursor.cursor_key == 'events')).cursor_value['items']) == 1
        assert session.scalar(select(OperationalAlert)) is None


def test_mid_cycle_longer_retention_restarts_scan_with_new_cutoff():
    factory = _factory(); seed(factory)
    # 停机/配置切换发生在任何删除之前，恢复不能继续旧的短保留期。
    from app.content.news_storage import cursor
    with factory.begin() as session:
        cursor(session, 'retention', {'status': 'running', 'startedAt': NOW.isoformat(),
            'cutoff': (NOW-timedelta(days=1)).isoformat(), 'retentionDays': 1, 'phase': 0, 'lastId': 0, 'counts': {}})
    cleanup_news(factory, Config(retention_days=730), NOW)
    with factory() as session: assert len(session.scalars(select(ContentDocument)).all()) == 2


def test_pending_snapshot_and_shared_announcement_batch_remain():
    from app.db.models import PageSnapshot
    from app.content.marts.content import ContentMartBuilder
    factory = _factory(); store = seed(factory, pending=True)
    with factory.begin() as session:
        docs = session.scalars(select(ContentDocument)).all()
        payload = {'marketNews': ContentMartBuilder.from_documents(docs)}
        session.add(PageSnapshot(product='market-explorer', snapshot_version='test', as_of=NOW, payload=payload))
        batch = session.scalar(select(ExternalFile))
        session.add(ExternalRecord(external_file_id=batch.id, row_number=3, record_type='content',
            payload={'contentType': 'announcement', 'source': 'sina-finance', 'publishedAt': '2020-01-01T00:00:00Z'}))
    cleanup_news(factory, Config(), NOW)
    with factory() as session: assert session.scalar(select(PageSnapshot)).payload == payload
    store.ack([r['queueId'] for r in store.pending()])
    cleanup_news(factory, Config(), NOW+timedelta(days=2))
    with factory() as session:
        assert session.scalar(select(ExternalRecord)).payload['contentType'] == 'announcement'
        assert session.scalar(select(ExternalFile)) is not None


def test_stopped_cleanup_makes_no_change():
    factory = _factory(); seed(factory)
    assert cleanup_news(factory, Config(), NOW, stopped=lambda: True)['status'] == 'stopped'
    with factory() as session: assert len(session.scalars(select(ContentDocument)).all()) == 2


def test_workbench_mentions_are_recalculated_without_new_delivery():
    from copy import deepcopy
    from app.db.models import PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot
    factory = _factory(); seed(factory)
    with factory.begin() as session:
        for row in session.scalars(select(ContentDocument)):
            row.securities = ['600519.SH'] if row.external_id == '2' else ['600000.SH']
        session.add(ContentDocument(source='other', dedupe_key='announcement', title='公告保留',
            content_type='announcement', published_at=NOW, source_url='https://example.com/a',
            copyright_status='link_only', securities=['600000.SH']))
        for symbol in ('600519.SH', '600000.SH'):
            payload = {'quote': {'mentions': 9, 'latestMention': '2020-01-01T00:00:00Z', 'price': 100},
                'dailyChart': {'announcements': [{'title': '不改变公告'}]}}
            session.add(PageSnapshot(product=f'stock-workbench:{symbol}', snapshot_version='v', as_of=NOW, payload=deepcopy(payload)))
            session.add(PublishedSnapshot(product=f'stock-workbench:{symbol}', version='v', as_of=NOW, stale=True, payload=deepcopy(payload)))
            session.add(StockWorkbenchSnapshot(ts_code=symbol, snapshot_version='v', as_of=NOW.date(), payload=deepcopy(payload)))
    cleanup_news(factory, Config(), NOW)
    with factory() as session:
        for model in (PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot):
            for row in session.scalars(select(model)):
                symbol = row.ts_code if model is StockWorkbenchSnapshot else row.product.split(':')[1]
                expected = 0 if symbol == '600519.SH' else 2
                assert row.payload['quote']['mentions'] == expected
                assert row.payload['quote']['latestMention'] == ('' if expected == 0 else NOW.isoformat())
                assert row.payload['quote']['price'] == 100
                assert row.payload['dailyChart']['announcements'] == [{'title': '不改变公告'}]


def test_resume_does_not_regress_news_published_after_cleanup_started():
    from app.db.models import PublishedSnapshot
    factory = _factory(); seed(factory)
    config = Config(cleanup_max_batches=1)
    assert cleanup_news(factory, config, NOW)['status'] == 'running'
    later = NOW+timedelta(hours=1)
    with factory.begin() as session:
        session.add(ContentDocument(source='sina-finance', external_id='new', dedupe_key='sina:new', title='新新闻',
            content_type='news', published_at=later, source_url='https://example.com/new',
            copyright_status='link_only', securities=['600519.SH']))
        session.add(PublishedSnapshot(product='stock-workbench:600519.SH', version='new', as_of=NOW,
            payload={'quote': {'mentions': 1, 'latestMention': later.isoformat(), 'price': 100}}))
    for _ in range(15):
        result = cleanup_news(factory, config, NOW+timedelta(hours=2))
        if result['status'] == 'completed': break
    assert result['status'] == 'completed'
    with factory() as session:
        quote = session.scalar(select(PublishedSnapshot)).payload['quote']
        assert quote['mentions'] == 1 and quote['latestMention'] == later.isoformat()
