"""新闻保留策略：同库事务、分页续跑，保留未交付/活跃异常及业务游标。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, func, cast
from sqlalchemy.dialects.postgresql import JSONB
from app.db.models import (ContentDocument, ExternalFile, ExternalRecord, JobCursor,
    OperationalAlert, PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot)
from app.db.models.news import NewsDelivery
from app.content.news_storage import JOB, SOURCE, cursor
from app.jobs.locks import data_publication_lock_key, lock_key
from app.content.content_policy import DISPLAY_STATUSES

MODELS = (ContentDocument, ExternalRecord, NewsDelivery, PageSnapshot,
          PublishedSnapshot, StockWorkbenchSnapshot)


def utc(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def expired(value, cutoff):
    try:
        return utc(value) < cutoff
    except (ValueError, TypeError, AttributeError):
        return False  # 时间无法确认时保守保留，不猜测并删除。


def pending(session, external_id):
    return session.scalar(select(NewsDelivery.id).where(NewsDelivery.source == SOURCE,
        NewsDelivery.external_id == external_id, NewsDelivery.delivered.is_(False)).limit(1)) is not None


def _snapshot(session, row, cutoff, as_of):
    payload = deepcopy(row.payload)
    kept = []
    for item in payload.get('marketNews', []):
        remove = item.get('source') == SOURCE and item.get('contentType') == 'news' and expired(item.get('publishedAt'), cutoff)
        # 快照只有Core id/URL；按当前Core记录检查未交付保护。
        if remove:
            document = session.scalar(select(ContentDocument).where(ContentDocument.source == SOURCE,
                ContentDocument.source_url == item.get('sourceUrl')).limit(1))
            if document and pending(session, document.external_id): remove = False
        if not remove: kept.append(item)
    if 'marketNews' in payload: payload['marketNews'] = kept
    product = getattr(row, 'product', '')
    symbol = row.ts_code if isinstance(row, StockWorkbenchSnapshot) else product.split(':', 1)[1] if product.startswith('stock-workbench:') else None
    if symbol and isinstance(payload.get('quote'), dict):
        linked = cast(ContentDocument.securities, JSONB).contains([symbol])
        count, latest = session.execute(select(func.count(ContentDocument.id), func.max(ContentDocument.published_at))
            .where(linked, ContentDocument.copyright_status.in_(DISPLAY_STATUSES), ContentDocument.published_at <= as_of)).one()
        payload['quote']['mentions'] = count
        payload['quote']['latestMention'] = utc(latest).isoformat() if latest else ''
    if payload != row.payload:
        row.payload = payload
        return 1
    return 0


def _step(session, state, config):
    phase = state['phase']; model = MODELS[phase]
    cutoff = utc(state['cutoff'])
    key = model.product if model is PublishedSnapshot else model.id
    after = str(state['lastId']) if model is PublishedSnapshot and state['lastId'] else state['lastId']
    if model is PublishedSnapshot and not after: after = ''
    statement = select(model).where(key > after).order_by(key).limit(config.cleanup_batch_size)
    if model is ContentDocument:
        statement = statement.where(model.source == SOURCE, model.content_type == 'news', model.published_at < cutoff)
    elif model is ExternalRecord:
        statement = statement.where(model.record_type == 'content',
            model.payload['source'].as_string() == SOURCE, model.payload['contentType'].as_string() == 'news')
    elif model is NewsDelivery:
        statement = statement.where(model.source == SOURCE, model.delivered.is_(True))
    rows = session.scalars(statement.with_for_update()).all()
    counts = dict(state.get('counts', {})); changed = 0
    for row in rows:
        if model is ContentDocument:
            if not pending(session, row.external_id): session.delete(row); changed += 1
        elif model is ExternalRecord:
            if expired(row.payload.get('publishedAt'), cutoff) and not pending(session, row.payload.get('externalId')):
                batch_id = row.external_file_id
                session.delete(row); session.flush(); changed += 1
                # 同一导入批次可能含有效新闻/公告等其他记录，仍有引用就不删。
                if session.scalar(select(ExternalRecord.id).where(ExternalRecord.external_file_id == batch_id).limit(1)) is None:
                    batch = session.get(ExternalFile, batch_id)
                    if batch:
                        session.delete(batch)
                        counts['external_file'] = counts.get('external_file', 0) + 1
        elif model is NewsDelivery:
            if expired(row.payload.get('publishedAt'), cutoff) and not pending(session, row.external_id):
                session.delete(row); changed += 1
        else:
            changed += _snapshot(session, row, cutoff, utc(state['checkedAt']))
    counts[model.__tablename__] = counts.get(model.__tablename__, 0) + changed
    state.update(counts=counts, lastId=(getattr(rows[-1], 'product') if model is PublishedSnapshot else rows[-1].id) if rows else 0)
    if len(rows) < config.cleanup_batch_size:
        state.update(phase=phase+1, lastId=0)


def _logs(session, config, now):
    cutoff = now-timedelta(days=config.log_retention_days)
    health = session.scalar(select(JobCursor).where(JobCursor.job_name == 'news-collector', JobCursor.cursor_key == SOURCE))
    alert = session.scalar(select(OperationalAlert).where(OperationalAlert.alert_key == 'news-collector'))
    active = (health and (health.cursor_value.get('error') or health.cursor_value.get('deliveryError'))) or (alert and alert.status != 'resolved')
    state = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == SOURCE))
    active = active or (state and state.cursor_value.get('lastError'))
    if not active:
        events = cursor(session, 'events', {'items': []})
        events.cursor_value = {'items': [e for e in events.cursor_value['items'] if not expired(e.get('at'), cutoff)]}
        if alert and alert.resolved_at and expired(alert.resolved_at, cutoff): session.delete(alert)
    retention_alert = session.scalar(select(OperationalAlert).where(OperationalAlert.alert_key == 'news-retention'))
    if retention_alert and retention_alert.status == 'resolved' and retention_alert.resolved_at and expired(retention_alert.resolved_at, cutoff):
        session.delete(retention_alert)


def cleanup_news(factory, config, now, *, stopped=lambda: False):
    """调用方持有news-collector锁；每批另取发布/存储事务锁，避免覆盖并发发布。"""
    now = utc(now)
    for _ in range(config.cleanup_max_batches):
        if stopped(): return {'status': 'stopped'}
        with factory.begin() as session:
            for key in (data_publication_lock_key(), JOB):
                if not session.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': lock_key(key)}):
                    return {'status': 'locked'}
            record = cursor(session, 'retention', {})
            state = dict(record.cursor_value)
            if state.get('status') != 'running':
                if state.get('completedAt') and now < utc(state['completedAt'])+timedelta(seconds=config.cleanup_interval_seconds):
                    return {'status': 'not_due', **{'lastRun': state}}
                state = {'status': 'running', 'startedAt': now.isoformat(),
                    'cutoff': (now-timedelta(days=config.retention_days)).isoformat(),
                    'retentionDays': config.retention_days, 'phase': 0, 'lastId': 0, 'counts': {}}
            # 配置改动后重新扫描，不能继续旧的、更短保留期。
            if state['retentionDays'] != config.retention_days:
                state.update(retentionDays=config.retention_days, cutoff=(now-timedelta(days=config.retention_days)).isoformat(), phase=0, lastId=0)
            state['checkedAt'] = now.isoformat()
            _step(session, state, config)
            if state['phase'] == len(MODELS):
                _logs(session, config, now)
                state.update(status='completed', completedAt=now.isoformat())
            record.cursor_value = state
        if state['status'] == 'completed': return state
    return state


def run_cleanup(factory, config, now, *, stopped=lambda: False):
    """清理失败单独告警，不使采集/网站停止；下一轮按原检查点重试。"""
    try:
        report = cleanup_news(factory, config, now, stopped=stopped)
    except Exception as exc:
        report = {'status': 'failed', 'code': 'NEWS_RETENTION_FAILED', 'errorType': type(exc).__name__}
    with factory.begin() as session:
        alert = session.scalar(select(OperationalAlert).where(OperationalAlert.alert_key == 'news-retention'))
        if report['status'] == 'failed':
            if alert is None:
                alert = OperationalAlert(alert_key='news-retention', alert_type='news_retention', severity='warning', first_triggered_at=now)
                session.add(alert)
            alert.status = 'active'; alert.last_triggered_at = now; alert.resolved_at = None
            alert.details = {'code': report['code'], 'errorType': report['errorType']}
        elif report['status'] == 'completed' and alert:
            alert.status = 'resolved'; alert.resolved_at = now
    return report
