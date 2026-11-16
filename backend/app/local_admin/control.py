"""本机进程共用的任务意图与执行回执；每次准入在 SQLite 内原子判断。"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from datetime import UTC, datetime


def now():
    return datetime.now(UTC).isoformat()


class ControlStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS task (
                name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, rule TEXT NOT NULL,
                generation INTEGER DEFAULT 1, revision INTEGER DEFAULT 1, recovered INTEGER DEFAULT 0,
                applied INTEGER DEFAULT 0, effective TEXT DEFAULT '', active INTEGER DEFAULT 0,
                started TEXT, finished TEXT, result TEXT, next_run TEXT, error TEXT, seen TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS execution (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, generation INTEGER NOT NULL,
                recovery INTEGER NOT NULL, started TEXT NOT NULL, finished TEXT, result TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS execution_by_task ON execution(name, id DESC)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def target(self, name, enabled, rule):
        self.targets([(name, enabled, rule)])

    def targets(self, targets):
        with self.connection() as db:
            db.executemany("""INSERT INTO task(name,enabled,rule) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, rule=excluded.rule,
                generation=generation+(enabled!=excluded.enabled), revision=revision+1, applied=0, error=NULL
                WHERE enabled!=excluded.enabled OR rule!=excluded.rule""", targets)

    def rows(self):
        with self.connection() as db:
            return {r["name"]: dict(r) for r in db.execute("SELECT * FROM task")}

    def get(self, name):
        with self.connection() as db:
            return dict(db.execute("SELECT * FROM task WHERE name=?", (name,)).fetchone())

    def admit(self, name, generation, *, recovery, revision=None):
        with self.connection() as db:
            started = now()
            changed = db.execute("""UPDATE task SET active=1, started=?, finished=NULL, result=NULL,
                recovered=CASE WHEN ? THEN generation ELSE recovered END
                WHERE name=? AND enabled AND active=0 AND generation=? AND (NOT ? OR recovered!=generation)
                AND (? IS NULL OR revision=?)""",
                (started, recovery, name, generation, recovery, revision, revision))
            if changed.rowcount:
                db.execute("INSERT INTO execution(name,generation,recovery,started) VALUES(?,?,?,?)",
                           (name, generation, recovery, started))
            return bool(changed.rowcount)

    def finish(self, name, result):
        with self.connection() as db:
            finished = now()
            db.execute("UPDATE execution SET result=?,finished=? WHERE name=? AND finished IS NULL AND result IS NULL",
                       (result, finished, name))
            db.execute("UPDATE task SET active=0,finished=?,result=? WHERE name=?", (finished, result, name))

    def recent(self, name, offset):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM execution WHERE name=? ORDER BY id DESC LIMIT 21 OFFSET ?", (name, offset))]

    def acknowledge(self, row, next_run=None, error=None):
        with self.connection() as db:
            db.execute("""UPDATE task SET applied=?, effective=?, next_run=?, error=?, seen=?
                WHERE name=? AND revision=? AND generation=? AND rule=?""",
                (0 if error else row["generation"], "" if error else row["rule"], next_run, error, now(),
                 row["name"], row["revision"], row["generation"], row["rule"]))

    def reset_instance(self, names, *, cold=False):
        with self.connection() as db:
            for name in names:
                db.execute("UPDATE execution SET result='进程退出，结束时间待确认' WHERE name=? AND finished IS NULL AND result IS NULL", (name,))
                # 已开始但失联的轮次保留消费标记，避免重放同一供应商请求。
                db.execute("""UPDATE task SET generation=generation+(? AND enabled AND NOT active AND NOT EXISTS(
                        SELECT 1 FROM execution e WHERE e.name=task.name AND e.generation=task.generation
                        AND e.finished IS NULL)),
                    result=CASE WHEN active THEN '上次进程退出，结果待确认' ELSE result END,
                    active=0, applied=0, effective='', next_run=NULL, seen=NULL WHERE name=?""", (cold, name))
