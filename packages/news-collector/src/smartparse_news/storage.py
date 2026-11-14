"""存储协议及可选SQLite实现；采集核心不依赖具体数据库。"""
from contextlib import contextmanager, closing
import json
import sqlite3
import uuid
from typing import Protocol, ContextManager


class StoreLocked(RuntimeError):
    pass


class Transaction(Protocol):
    def load(self) -> dict: ...
    def save(self, state: dict) -> None: ...
    def append(self, record: dict, now: str) -> None: ...
    def event(self, now: str, code: str) -> None: ...
    def expire(self, cutoff: str) -> None: ...


class Store(Protocol):
    def transaction(self) -> ContextManager[Transaction]: ...
    def status(self) -> dict: ...
    def pending(self, limit: int = 500) -> list[dict]: ...
    def ack(self, ids: list[int]) -> None: ...


class SQLiteTransaction:
    def __init__(self, db): self.db = db
    def load(self): return json.loads(self.db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])
    def save(self, state): self.db.execute('UPDATE state SET value=? WHERE id=1', (json.dumps(state),))
    def append(self, record, now):
        prior = self.db.execute('SELECT payload FROM queue WHERE external_id=? ORDER BY id DESC LIMIT 1', (record['externalId'],)).fetchone()
        if prior and json.loads(prior[0])['contentHash'] == record['contentHash']: return
        self.db.execute('INSERT INTO queue(external_id,hash,payload,fetched_at) VALUES(?,?,?,?)',
            (record['externalId'], record['contentHash']+':'+uuid.uuid4().hex, json.dumps(record, ensure_ascii=False), now))
    def event(self, now, code): self.db.execute('INSERT INTO events(occurred_at,code) VALUES(?,?)', (now, code))
    def expire(self, cutoff):
        self.db.execute('DELETE FROM queue WHERE delivered=1 AND fetched_at<?', (cutoff,))
        self.db.execute('DELETE FROM events WHERE occurred_at<?', (cutoff,))


class SQLiteStore:
    def __init__(self, path, initial):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY, external_id TEXT NOT NULL,
                hash TEXT NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                fetched_at TEXT NOT NULL, UNIQUE(external_id, hash));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, occurred_at TEXT, code TEXT);''')
            db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)', (json.dumps(initial),))
    def connect(self): return sqlite3.connect(self.path, timeout=1)
    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db, db:
            try: db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as exc: raise StoreLocked() from exc
            yield SQLiteTransaction(db)
    def status(self):
        with closing(self.connect()) as db: return SQLiteTransaction(db).load()
    def pending(self, limit=500):
        with closing(self.connect()) as db:
            return [{'queueId': i, 'record': json.loads(p)} for i, p in db.execute(
                'SELECT id,payload FROM queue WHERE delivered=0 ORDER BY id LIMIT ?', (limit,))]
    def ack(self, ids):
        with closing(self.connect()) as db, db:
            db.executemany('UPDATE queue SET delivered=1 WHERE id=?', [(i,) for i in ids])
