from datetime import datetime, timezone, timedelta
import json
import pytest
from sqlalchemy import select
from app.config import Settings
from app.db.models import ContentDocument, OperationalAlert
from app.content.routes import read_news
from app.content.news_runtime import save_health, run_news
from smartparse_news import Collector, Config
from backend.tests.jobs.test_build_and_publish_integration import _factory

NOW = datetime(2026, 11, 10, 2, tzinfo=timezone.utc)


def test_keyset_same_second_and_rights():
    factory = _factory()
    with factory.begin() as session:
        for i in range(4):
            session.add(ContentDocument(source='sina-finance', dedupe_key=str(i), title=str(i),
                content_type='news', published_at=NOW, source_url=f'https://example.com/{i}',
                copyright_status='link_only' if i < 3 else 'restricted'))
    first = read_news(factory, limit=2, now=NOW)
    second = read_news(factory, first['nextCursor'], 2, NOW)
    assert [r['title'] for r in first['items']+second['items']] == ['2', '1', '0']
    assert first['items'][0]['time'] == '10:00'
    assert second['nextCursor'] is None
    assert 'summary' not in first['items'][0]
    with pytest.raises(ValueError):
        read_news(factory, 'invalid', now=NOW)


def test_health_deduplicates_recovers_and_detects_shutdown_delay():
    factory = _factory(); config = Config()
    state = dict(failures=2, lastError='SCHEMA_CHANGED', lastSuccessAt=NOW.isoformat(), latestPublishedAt=NOW.isoformat())
    save_health(factory, state, config, NOW)
    save_health(factory, state, config, NOW)
    with factory() as session:
        assert len(session.scalars(select(OperationalAlert)).all()) == 1
    state.update(failures=0, lastError=None)
    save_health(factory, state, config, NOW)
    with factory() as session:
        assert session.scalar(select(OperationalAlert)).status == 'resolved'
    assert read_news(factory, now=NOW+timedelta(days=1))['health']['status'] == 'warning'


def test_collection_success_cannot_resolve_pending_delivery_failure():
    factory = _factory(); config = Config()
    state = dict(failures=0, lastError=None, lastSuccessAt=NOW.isoformat())
    save_health(factory, state, config, NOW, 'PUBLICATION_FAILED')
    save_health(factory, state, config, NOW)
    with factory() as session:
        assert session.scalar(select(OperationalAlert)).status == 'active'
    state['lastSuccessAt'] = None
    save_health(factory, state, config, NOW, delivery_ok=True)
    with factory() as session:
        assert session.scalar(select(OperationalAlert)).status == 'resolved'


def test_ingestion_revision_allows_title_revert_without_duplicate_document(tmp_path):
    from app.ingestion.external_import import ExternalImporter
    from app.connectors.external_files.reader import ExternalFileReader
    factory = _factory()
    importer = ExternalImporter(factory, ExternalFileReader(tmp_path))
    for revision, title in enumerate(['A', 'B', 'A']):
        record = dict(schemaVersion='1.0', recordType='content', contentType='news',
            source='sina-finance', externalId='1', title=title, publishedAt=NOW.isoformat(),
            sourceUrl='https://example.com/1', copyrightStatus='link_only', ingestionRevision=str(revision))
        path = tmp_path / f'{revision}.jsonl'; path.write_text(json.dumps(record), encoding='utf-8')
        assert importer.import_file(path).status == 'imported'
        with factory() as session:
            documents = session.scalars(select(ContentDocument)).all()
            assert len(documents) == 1 and documents[0].title == title


def test_failed_publication_preserves_delivery_then_retries(tmp_path, monkeypatch):
    from app.content import news_runtime as runtime
    factory = _factory(); engine = factory.kw['bind']
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    monkeypatch.setattr(runtime, 'create_engine_and_session', lambda _, **kw: (engine, factory))
    config = tmp_path/'news.json'; config.write_text('{}')
    settings = Settings(database_url='sqlite:///controlled', news_config_path=config,
        news_state_path=tmp_path/'state.sqlite', external_import_path=tmp_path/'imports')
    def seed_run(self, now):
        # Test-only no-network payload; no imported fixture data in production.
        self.fetch = lambda _: {'result': {'status': {'code': 0}, 'data': {'feed': {'list': [
            {'id': 1, 'create_time': now.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
             'rich_text': '【受控测试】正文'}]}}}}
        self.sleep = lambda _: None
        return original(self, now)
    original = Collector.run
    monkeypatch.setattr(Collector, 'run', seed_run)
    monkeypatch.setattr(runtime, 'rebuild_content_marts', lambda _, **kw: {'status': 'blocked'})
    assert run_news(settings, publish=True)['status'] == 'delivery_failed'
    from app.content.news_storage import DatabaseNewsStore
    assert len(DatabaseNewsStore(factory).pending()) == 1
    def publish(_, **kw):
        assert 'sleep' not in kw['connection_options']
        return {'status': 'completed'}
    monkeypatch.setattr(runtime, 'rebuild_content_marts', publish)
    report = run_news(settings, publish=True, sleep=lambda _: None)
    assert report['requestCount'] == 0 and report['pendingCount'] == 0
    with factory() as session:
        assert len(session.scalars(select(ContentDocument)).all()) == 1
