"""独立包Store协议的宿主实现；所有正常运行状态位于同一数据库。"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
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
            if session.bind.dialect.name == 'postgresql' and not session.scalar(
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

    def migrate_legacy(self, path):
        """调用者持有宿主任务锁；只读旧库，主库整批事务+标记，绝不删除原文件。"""
        with self.transaction() as tx:
            session = tx.session
            marker = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == 'legacy-migration'))
            if marker:
                if marker.cursor_value['status'] == 'no_legacy' and path.exists():
                    raise RuntimeError('NEWS_LATE_LEGACY_CONFLICT')
                return dict(marker.cursor_value)
            state = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == SOURCE))
            if not path.exists():
                result = {'status': 'no_legacy', 'rows': 0, 'pending': 0}
            else:
                if state or session.scalar(select(NewsDelivery.id).limit(1)):
                    raise RuntimeError('NEWS_MIGRATION_TARGET_NOT_EMPTY')
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as old:
                    old.execute('BEGIN')
                    if old.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('NEWS_LEGACY_INTEGRITY_FAILED')
                    saved = json.loads(old.execute('SELECT value FROM state WHERE id=1').fetchone()[0])
                    if not initial_state().keys() <= saved.keys(): raise RuntimeError('NEWS_LEGACY_STATE_INVALID')
                    rows = old.execute('SELECT external_id,payload,delivered,fetched_at FROM queue ORDER BY id').fetchall()
                    events = old.execute('SELECT occurred_at,code FROM events ORDER BY id').fetchall()
                tx.save(saved)
                for external, payload, delivered, fetched in rows:
                    record = json.loads(payload)
                    if record.get('externalId') != external or record.get('source') != SOURCE or delivered not in (0, 1):
                        raise RuntimeError('NEWS_LEGACY_ROW_INVALID')
                    session.add(NewsDelivery(source=SOURCE, external_id=external, payload=record,
                        delivered=bool(delivered), fetched_at=datetime.fromisoformat(fetched)))
                for at, code in events: tx.event(at, code)
                session.flush()
                actual = session.scalars(select(NewsDelivery).where(NewsDelivery.source == SOURCE).order_by(NewsDelivery.id)).all()
                expected = [(external, json.loads(payload), bool(delivered), datetime.fromisoformat(fetched).isoformat()) for external, payload, delivered, fetched in rows]
                observed = [(r.external_id, r.payload, r.delivered,
                    (r.fetched_at if r.fetched_at.tzinfo else r.fetched_at.replace(tzinfo=timezone.utc)).isoformat()) for r in actual]
                if expected != observed or tx.load() != saved: raise RuntimeError('NEWS_MIGRATION_VERIFICATION_FAILED')
                result = {'status': 'migrated', 'rows': len(rows), 'pending': sum(not r[2] for r in rows),
                    'nextId': saved['nextId'], 'fingerprint': hashlib.sha256(json.dumps([saved, rows, events],sort_keys=True).encode()).hexdigest()}
            cursor(session, 'legacy-migration', result)
            return result
