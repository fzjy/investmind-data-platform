"""独立包Store协议的宿主实现；所有正常运行状态位于同一数据库。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import select, delete, update, text
from app.db.models import JobCursor
from app.db.models.news import NewsDelivery
from app.jobs.locks import lock_key
from smartparse_news.collector import initial_state
from smartparse_news.storage import StoreLocked

JOB = 'news-collector-store'
SOURCE = 'sina-finance'


def cursor(session, key, initial):
    row = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == key))
    if row is None:
        row = JobCursor(job_name=JOB, cursor_key=key, cursor_value=initial)
        session.add(row); session.flush()
    return row


class DatabaseTransaction:
    def __init__(self, session): self.session = session
    def load(self): return dict(cursor(self.session, SOURCE, initial_state()).cursor_value)
    def save(self, state): cursor(self.session, SOURCE, initial_state()).cursor_value = dict(state)
    def append(self, record, now):
        previous = self.session.scalar(select(NewsDelivery).where(NewsDelivery.source == SOURCE,
            NewsDelivery.external_id == record['externalId']).order_by(NewsDelivery.id.desc()).limit(1))
        if previous and previous.payload['contentHash'] == record['contentHash']: return
        self.session.add(NewsDelivery(source=SOURCE, external_id=record['externalId'], payload=record,
            delivered=False, fetched_at=datetime.fromisoformat(now)))
        self.session.flush()
    def event(self, now, code):
        row = cursor(self.session, 'events', {'items': []})
        row.cursor_value = {'items': [*row.cursor_value['items'], {'at': now, 'code': code}]}
    def expire(self, cutoff):
        # 宿主由news_retention统一按发布时间分批清理，不能按抓取时间提前删关联记录。
        pass


class DatabaseNewsStore:
    def __init__(self, factory): self.factory = factory
    @contextmanager
    def transaction(self):
        with self.factory.begin() as session:
            if not session.scalar(
                text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': lock_key(JOB)}):
                raise StoreLocked()
            yield DatabaseTransaction(session)
    def status(self):
        with self.transaction() as tx: return tx.load()
    def pending(self, limit=500):
        with self.factory() as session:
            return [{'queueId': row.id, 'record': row.payload} for row in session.scalars(select(NewsDelivery)
                .where(NewsDelivery.source == SOURCE, NewsDelivery.delivered.is_(False)).order_by(NewsDelivery.id).limit(limit))]
    def ack(self, ids, session=None):
        from contextlib import nullcontext
        with (self.factory.begin() if session is None else nullcontext(session)) as session:
            session.execute(update(NewsDelivery).where(NewsDelivery.source == SOURCE, NewsDelivery.id.in_(ids)).values(delivered=True))
