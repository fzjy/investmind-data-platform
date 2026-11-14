from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Event
import json
import pytest
from sqlalchemy import select
from smartparse_news import Collector, Config
from app.content.news_storage import DatabaseNewsStore
from app.content.news_ingestion import ingest_news
from app.db.models import ContentDocument, JobCursor
from app.db.models.news import NewsDelivery
from backend.tests.jobs.test_build_and_publish_integration import _factory

NOW = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
RESPONSE = {'result': {'status': {'code': 0}, 'data': {'feed': {'list': [
    {'id': 2, 'create_time': '2026-11-10 10:59:00', 'rich_text': '【迁移测试】只保存标题'},
    {'id': 1, 'create_time': '2026-11-10 10:59:00', 'rich_text': '【第二条】原文'}]}}}}


def test_legacy_migration_preserves_state_rows_pending_and_is_idempotent(tmp_path):
    config = Config(page_size=2, max_pages=1, request_interval_seconds=0)
    path = tmp_path/'legacy.sqlite'
    legacy = Collector(path, config, fetch=lambda _: RESPONSE)
    legacy.run(NOW); legacy.ack([legacy.pending()[0]['queueId']])
    before = path.read_bytes()
    factory = _factory(); store = DatabaseNewsStore(factory)
    result = store.migrate_legacy(path)
    assert result['rows'] == 2 and result['pending'] == 1
    assert store.status() == legacy.status()
    assert store.pending()[0]['record'] == legacy.pending()[0]['record']
    assert store.migrate_legacy(path) == result and before == path.read_bytes()
    assert Collector(None, config, store=store, fetch=lambda _: pytest.fail('not due')).run(NOW)['requestCount'] == 0


def test_corrupt_legacy_never_initializes_or_clears(tmp_path):
    path = tmp_path/'broken.sqlite'; path.write_bytes(b'not sqlite')
    factory = _factory(); store = DatabaseNewsStore(factory)
    with pytest.raises(Exception): store.migrate_legacy(path)
    with factory() as session:
        assert session.scalar(select(JobCursor)) is None
    assert path.read_bytes() == b'not sqlite'


def test_invalid_second_legacy_row_rolls_back_entire_migration(tmp_path):
    import sqlite3
    from contextlib import closing
    path = tmp_path/'legacy.sqlite'
    legacy = Collector(path, Config(request_interval_seconds=0), fetch=lambda _: RESPONSE)
    legacy.run(NOW)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE queue SET payload='{}' WHERE id=2")
    factory = _factory(); store = DatabaseNewsStore(factory)
    with pytest.raises(RuntimeError): store.migrate_legacy(path)
    with factory() as session:
        assert session.scalar(select(NewsDelivery)) is None
        assert session.scalar(select(JobCursor)) is None


def test_later_legacy_file_is_reported_instead_of_silently_skipped(tmp_path):
    path = tmp_path/'late.sqlite'; store = DatabaseNewsStore(_factory())
    assert store.migrate_legacy(path)['status'] == 'no_legacy'
    legacy = Collector(path, Config(request_interval_seconds=0), fetch=lambda _: RESPONSE)
    legacy.run(NOW)
    with pytest.raises(RuntimeError, match='NEWS_LATE_LEGACY_CONFLICT'):
        store.migrate_legacy(path)
    assert len(legacy.pending()) == 2


def test_delivery_ack_and_health_recovery_roll_back_together(tmp_path, monkeypatch):
    from app.content.news_runtime import save_health
    factory = _factory(); store = DatabaseNewsStore(factory)
    c = Collector(None, Config(request_interval_seconds=0), store=store, fetch=lambda _: RESPONSE)
    c.run(NOW); ids = [r['queueId'] for r in c.pending()]
    state = c.status()
    save_health(factory, state, Config(), NOW, 'PUBLICATION_FAILED')
    with pytest.raises(RuntimeError):
        with factory.begin() as session:
            store.ack(ids, session=session)
            save_health(factory, state, Config(), NOW, delivery_ok=True, session=session)
            raise RuntimeError('controlled failure before commit')
    assert len(c.pending()) == 2
    from app.db.models import OperationalAlert
    with factory() as session:
        assert session.scalar(select(OperationalAlert)).status == 'active'


def test_database_store_collects_without_files_and_direct_delivery_is_idempotent(tmp_path):
    factory = _factory(); store = DatabaseNewsStore(factory)
    store.migrate_legacy(tmp_path/'absent.sqlite')
    c = Collector(None, Config(page_size=3, request_interval_seconds=0), store=store, fetch=lambda _: RESPONSE)
    c.run(NOW); pending = c.pending()
    assert ingest_news(factory, pending) == 2
    assert ingest_news(factory, pending) == 2
    with factory() as session:
        assert len(session.scalars(select(ContentDocument)).all()) == 2
        assert all(row.summary is None for row in session.scalars(select(ContentDocument)))
    c.ack([e['queueId'] for e in pending]); assert c.pending() == []
    assert list(tmp_path.iterdir()) == []


def test_stop_prevents_next_supplier_request(tmp_path):
    store = DatabaseNewsStore(_factory())
    c = Collector(None, Config(), store=store, stopped=lambda: True, fetch=lambda _: pytest.fail('must not fetch'))
    assert c.run(NOW)['requestCount'] == 0


def test_worker_retries_without_blocking_event_loop_and_stops(tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace
    from app.content.news_worker import news_loop
    config = tmp_path/'config.json'; config.write_text('{"interval_seconds":1,"worker_retry_seconds":1}')
    stop = Event(); calls = []
    def runner(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(.1); raise RuntimeError('controlled failure')
        stop.set(); return {'status': 'completed', 'requestCount': 0}
    async def scenario():
        task = asyncio.create_task(news_loop(stop, settings_loader=lambda: SimpleNamespace(news_config_path=config), runner=runner))
        await asyncio.sleep(.02)
        assert not task.done()  # 阻塞runner不阻塞HTTP所在事件循环。
        await asyncio.wait_for(task, 3)
    asyncio.run(scenario()); assert len(calls) == 2


def test_disabled_worker_does_not_collect_or_migrate(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from app.content.news_worker import news_loop
    path = tmp_path/'disabled.json'; path.write_text('{"enabled":false}')
    stop = Event()
    async def scenario():
        task = asyncio.create_task(news_loop(stop, settings_loader=lambda: SimpleNamespace(news_config_path=path), runner=lambda *a, **kw: pytest.fail('disabled')))
        await asyncio.sleep(.03); stop.set(); await asyncio.wait_for(task, 1)
    asyncio.run(scenario())
