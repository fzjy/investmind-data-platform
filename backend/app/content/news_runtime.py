"""独立采集包到既有内容导入/发布的薄适配层。"""
from dataclasses import asdict
from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import select
from app.db.models import JobCursor, OperationalAlert
from app.db.session import create_engine_and_session
from app.content.marts.content_runtime import rebuild_content_marts
from app.jobs.locks import job_lock_for_engine

from app.content.news_storage import DatabaseNewsStore
from app.content.news_ingestion import ingest_news
from app.content.news_retention import run_cleanup

JOB = 'news-collector'


def save_health(factory, state, config, now, delivery_error=None, delivery_ok=False, session=None):
    with (factory.begin() if session is None else nullcontext(session)) as session:
        row = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == 'sina-finance'))
        if row is None:
            row = JobCursor(job_name=JOB, cursor_key='sina-finance'); session.add(row)
        prior_delivery = (row.cursor_value or {}).get('deliveryError')
        delivery_error = delivery_error or (None if delivery_ok else prior_delivery)
        error = delivery_error or state.get('lastError')
        active = bool(delivery_error or error == 'SOURCE_STALE' or state.get('failures', 0) >= config.failure_threshold)
        row.cursor_value = {**state, 'alertActive': active, 'error': error,
                            'deliveryError': delivery_error,
                            'staleHours': config.stale_hours, 'checkedAt': now.isoformat()}
        from app.acquisition.adapters.content import record_news
        record_news(session, state, now, delivery_error)
        alert = session.scalar(select(OperationalAlert).where(OperationalAlert.alert_key == JOB))
        if active:
            if alert is None:
                alert = OperationalAlert(alert_key=JOB, alert_type='news_collection', severity='warning',
                    first_triggered_at=now); session.add(alert)
            alert.status = 'active'; alert.last_triggered_at = now; alert.resolved_at = None
            alert.details = {'source': 'sina-finance', 'code': error,
                             'lastSuccessAt': state.get('lastSuccessAt'),
                             'action': '检查来源结构/连接后重试，保留原游标和已入库内容'}
        elif alert is not None and (state.get('lastSuccessAt') or delivery_ok) and not error:
            alert.status = 'resolved'; alert.resolved_at = now


def run_news(settings, *, publish=False, dry_run=False, stopped=lambda: False, sleep=None):
    from smartparse_news import Collector, Config
    config = Config.read(settings.news_config_path)
    if not config.enabled and not dry_run:
        return {'status': 'disabled', 'requestCount': 0}
    if dry_run:
        return {'status': 'planned', 'requestCount': 0, 'source': 'sina-finance',
                'config': asdict(config), 'publish': publish}
    if not settings.database_url:
        return {'status': 'blocked', 'code': 'DATABASE_REQUIRED', 'requestCount': 0}
    database_options = {'connect_timeout': config.database_connect_timeout_seconds,
        'options': f'-c statement_timeout={config.database_statement_timeout_seconds*1000} -c lock_timeout={config.database_lock_timeout_seconds*1000}'}
    engine, factory = create_engine_and_session(settings.database_url, connection_options=database_options)
    lock = job_lock_for_engine(engine)
    acquired = lock.try_acquire(JOB)
    if not acquired:
        engine.dispose()
        return {'status': 'locked', 'requestCount': 0}
    now = datetime.now(timezone.utc)
    report = {'status': 'failed', 'requestCount': 0}
    try:
        store = DatabaseNewsStore(factory)
        retention = run_cleanup(factory, config, now, stopped=stopped)
        options = {'sleep': sleep} if sleep is not None else {}
        collector = Collector(config, store=store, stopped=stopped, **options)
        report = collector.run(now)
        report['retention'] = retention
        report['storage'] = 'postgresql'
        state = collector.status()
        save_health(factory, state, config, now)
        if publish and not stopped():
            pending = collector.pending()
            if pending:
                imported = ingest_news(factory, pending)
                publication = rebuild_content_marts(settings, connection_options=database_options)
                report['imported'] = imported
                report['publication'] = publication
                if publication['status'] not in ('completed', 'no_change'):
                    save_health(factory, state, config, now, 'PUBLICATION_FAILED')
                    report['status'] = 'delivery_failed'
                else:
                    with factory.begin() as session:
                        store.ack([entry['queueId'] for entry in pending], session=session)
                        save_health(factory, state, config, now, delivery_ok=True, session=session)
        report['pendingCount'] = len(collector.pending())
        return report
    except Exception as exc:
        try:
            save_health(factory, collector.status() if 'collector' in locals() else {}, config, now, 'DELIVERY_FAILED')
        except Exception:
            pass
        code = str(exc) if str(exc) in ('NEWS_LATE_LEGACY_CONFLICT', 'NEWS_MIGRATION_TARGET_NOT_EMPTY', 'NEWS_MIGRATION_VERIFICATION_FAILED') else 'NEWS_RUN_FAILED'
        return {**report, 'status': 'failed', 'code': code, 'errorType': type(exc).__name__}
    finally:
        lock.release(JOB)
        engine.dispose()
