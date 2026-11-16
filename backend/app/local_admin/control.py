"""PostgreSQL 本机管理锁、任务意图与执行历史。"""
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text, update

from app.db.models.local_admin import LocalAdminExecution as Execution, LocalAdminTask as Task


class OwnerLost(RuntimeError):
    pass


def api_row(record):
    return {col.name: value.isoformat() if isinstance(value, datetime) else value
            for col in record.__table__.columns if col.name != "owner"
            for value in [getattr(record, col.name)]}


class ControlStore:
    def __init__(self, session_factory, owner=None):
        self.factory = session_factory
        self.owner = owner
        self.connection = None

    def acquire(self):
        with self.factory() as session:
            connection = session.get_bind().connect()
        try:
            owner = "investmind-admin-" + uuid4().hex
            connection.execute(text("SELECT set_config('application_name', :owner, false)"), {"owner": owner})
            if not connection.scalar(text("SELECT pg_try_advisory_lock(151710, 1)")):
                raise OwnerLost("已有管理进程持有数据库锁")
            connection.commit()
            self.owner = owner
            self.connection = connection
        except BaseException:
            connection.invalidate()
            connection.close()
            raise

    def release(self):
        if self.connection is not None:
            self.connection.invalidate()
            self.connection.close()
            self.connection = None

    @contextmanager
    def transaction(self):
        with self.factory.begin() as session:
            # 将跨任务的恢复与领取排序，接管者等待先前已获准的事务提交。
            session.execute(text("SELECT pg_advisory_xact_lock(151710, 2)"))
            if self.owner is None or not session.scalar(text("""
                SELECT EXISTS (SELECT FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                WHERE a.datname=current_database() AND a.application_name=:owner
                AND l.locktype='advisory' AND l.granted AND l.classid=151710
                AND l.objid=1 AND l.objsubid=2)
            """), {"owner": self.owner}):
                raise OwnerLost("管理进程已失去数据库锁")
            yield session

    def target(self, name, enabled, rule):
        self.targets([(name, enabled, rule)])

    def targets(self, targets):
        with self.transaction() as session:
            for name, enabled, rule in targets:
                row = session.get(Task, name)
                if row is None:
                    session.add(Task(name=name, enabled=enabled, rule=rule, owner=self.owner))
                elif row.enabled != enabled or row.rule != rule:
                    row.generation += int(row.enabled != enabled)
                    row.revision += 1
                    row.enabled, row.rule = enabled, rule
                    row.applied, row.error = 0, None

    def rows(self):
        with self.transaction() as session:
            return {r.name: api_row(r) for r in session.scalars(select(Task))}

    def get(self, name):
        with self.transaction() as session:
            return api_row(session.get(Task, name))

    def admit(self, name, generation, *, recovery, revision=None):
        with self.transaction() as session:
            started = datetime.now(UTC)
            claim = update(Task).where(Task.name == name, Task.enabled, ~Task.active,
                                       Task.owner == self.owner, Task.generation == generation)
            if recovery:
                claim = claim.where(Task.recovered != generation).values(recovered=generation)
            if revision is not None:
                claim = claim.where(Task.revision == revision)
            claim = claim.values(active=True, started=started, finished=None, result=None)
            if session.scalar(claim.returning(Task.name)) is None:
                return None
            execution = Execution(name=name, generation=generation, recovery=recovery,
                                  started=started, owner=self.owner)
            session.add(execution)
            session.flush()
            return execution.id

    def finish(self, name, execution_id, result):
        with self.transaction() as session:
            execution = session.get(Execution, execution_id)
            if (execution is None or execution.name != name or execution.owner != self.owner
                    or execution.result is not None or execution.finished is not None):
                return
            row = session.get(Task, name)
            execution.finished = row.finished = datetime.now(UTC)
            execution.result = row.result = result
            row.active = False

    def recent(self, name, offset):
        with self.transaction() as session:
            return [api_row(r) for r in session.scalars(select(Execution).where(Execution.name == name)
                    .order_by(Execution.id.desc()).limit(21).offset(offset))]

    def acknowledge(self, row, next_run=None, error=None):
        with self.transaction() as session:
            task = session.get(Task, row["name"])
            if all(getattr(task, key) == row[key] for key in ("revision", "generation", "rule")):
                task.applied = 0 if error else task.generation
                task.effective = "" if error else task.rule
                task.next_run = datetime.fromisoformat(next_run) if next_run else None
                task.error, task.seen = error, datetime.now(UTC)

    def reset_instance(self, names, *, cold=False):
        with self.transaction() as session:
            for name in names:
                task = session.get(Task, name)
                latest = session.scalar(select(Execution).where(
                    Execution.name == name, Execution.generation == task.generation
                ).order_by(Execution.id.desc()).limit(1))
                session.execute(update(Execution).where(
                    Execution.name == name, Execution.finished.is_(None), Execution.result.is_(None)
                ).values(result="进程退出，结束时间待确认"))
                if cold and task.enabled and not task.active and (latest is None or latest.finished is not None):
                    task.generation += 1
                if task.active:
                    task.result = "上次进程退出，结果待确认"
                task.owner = self.owner
                task.active, task.applied, task.effective = False, 0, ""
                task.next_run = task.seen = None
