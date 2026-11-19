"""只读Core新闻与持久运维状态；不调用供应商。"""
import base64
import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, or_, select
from app.db.models import ContentDocument, JobCursor, OperationalAlert
from app.content.dataset_query import visible_documents
from app.content.marts.content import ContentMartBuilder


def read_news(factory, cursor=None, limit=20, now=None):
    now = now or datetime.now(timezone.utc)
    query = visible_documents(now, content_type="news")
    if cursor:
        try:
            stamp, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            stamp = datetime.fromisoformat(stamp)
            if stamp.tzinfo is None or type(identity) is not int or identity < 1:
                raise ValueError()
        except Exception as exc:
            raise ValueError('INVALID_CURSOR') from exc
        query = query.where(or_(ContentDocument.published_at < stamp,
            and_(ContentDocument.published_at == stamp, ContentDocument.id < identity)))
    with factory() as session:
        rows = session.scalars(query.limit(limit+1)).all()
        state = session.scalar(select(JobCursor).where(JobCursor.job_name == 'news-collector', JobCursor.cursor_key == 'sina-finance'))
        health = dict(state.cursor_value) if state else {}
        cleanup = session.scalar(select(JobCursor).where(JobCursor.job_name == 'news-collector-store', JobCursor.cursor_key == 'retention'))
        retention = dict(cleanup.cursor_value) if cleanup else {}
        cleanup_alert = session.scalar(select(OperationalAlert).where(OperationalAlert.alert_key == 'news-retention', OperationalAlert.status == 'active'))
        cleanup_failed = cleanup_alert is not None
    visible = rows[:limit]
    items = ContentMartBuilder.from_documents(visible)
    for item, row in zip(items, visible):
        stamp = row.published_at
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        item['publishedAt'] = stamp.isoformat()
        item['time'] = stamp.astimezone(timezone(timedelta(hours=8))).strftime('%H:%M')
        item['source'] = '新浪财经' if item['source'] == 'sina-finance' else item['source']
    next_cursor = None
    if len(rows) > limit:
        last = visible[-1]
        stamp = last.published_at
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        next_cursor = base64.urlsafe_b64encode(json.dumps([stamp.isoformat(), last.id]).encode()).decode()
    latest = health.get('latestPublishedAt')
    delayed = bool(latest and now-datetime.fromisoformat(latest) > timedelta(hours=health.get('staleHours', 12)))
    return {'items': items, 'nextCursor': next_cursor, 'health': {
        'status': 'warning' if health.get('alertActive') or delayed or cleanup_failed else 'pending' if not health else 'ok',
        'code': 'SOURCE_STALE' if delayed else health.get('error') or ('NEWS_RETENTION_FAILED' if cleanup_failed else None),
        'retention': retention,
        'lastSuccessAt': health.get('lastSuccessAt'), 'latestPublishedAt': latest,
        'catchingUp': bool(health.get('nextId'))}}


def create_router(factory):
    router = APIRouter()

    @router.get('/api/data/market-news')
    @router.get('/api/v1/news')
    def market_news(cursor: str | None = Query(default=None, max_length=300), limit: int = Query(default=20, ge=1, le=50)):
        try:
            return read_news(factory, cursor, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='新闻游标无效') from exc

    return router
