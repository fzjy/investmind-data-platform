"""仅受控数据库造数与自有恢复容器；不参与产品恢复实现。"""
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
import uuid

from sqlalchemy import select
from testcontainers.community.postgres import PostgresContainer

from app.db.base import Base
from app.db.models import IngestionBatch, SourcePayload, JobRun, JobFailure, JobCursor, MacroObservation
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher
from app.ops.backup.config import parse_target
from app.ops.backup.inventory import read_inventory, snapshot_inventory
from app.ops.backup.tools import OWNER_LABEL
from backend.tests.jobs.test_build_and_publish_integration import _factory, _seed_complete_core_sample, _real_build_and_publish_job
from backend.tests.jobs.test_macro_publish import setup, CATALOG, AS_OF


SYMBOLS = ("600519.SH", *(f"{600000 + index:06d}.SH" for index in range(1, 50)))


def seed_backup_database(factory):
    engine = factory.kw["bind"]
    seed = _factory()
    _seed_complete_core_sample(seed)
    try:
        with seed.kw["bind"].connect() as source, engine.begin() as target:
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in source.execute(select(table)).mappings()]
                if not rows:
                    continue
                expanded = []
                for index, symbol in enumerate(SYMBOLS):
                    for row in rows:
                        record = {key: value for key, value in row.items() if key != "id"}
                        record["ts_code"] = symbol
                        if table.name == "security":
                            record.update(symbol=symbol[:6], name=f"受控股票{index}", board="主板", data={"industry": "食品饮料", "region": "贵州"})
                        expanded.append(record)
                target.execute(table.insert(), expanded)
    finally:
        seed.kw["bind"].dispose()
    result = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(date(2026, 8, 24), SYMBOLS))
    assert result.status == "completed"
    macro_engine, _, _, _, _, outcomes = setup(20)
    try:
        with macro_engine.connect() as source, engine.begin() as target:
            rows = [dict(row) for row in source.execute(select(MacroObservation.__table__)).mappings()]
            for row in rows:
                row.pop("id")
            # 同一期的旧修订仍存在，但真实发布只选择可见最新版。
            older = {**rows[0], "revision": "older", "value": 0, "released_at": AS_OF.replace(day=18)}
            target.execute(MacroObservation.__table__.insert(), [older, *rows])
    finally:
        macro_engine.dispose()
    history = SqlMacroSyncHistory(factory)
    run_id = history.start("backup-controlled", AS_OF, CATALOG.catalog_version, CATALOG.catalog_hash, "test", 1)
    report = MacroPublishJob(MacroMartBuilder(MacroRepository(factory), catalog=CATALOG), SnapshotPublisher(factory), history).publish(run_id, AS_OF, CATALOG, outcomes)
    assert report.status == "completed"
    with factory.begin() as session:
        session.add(IngestionBatch(id="backup-raw", source="controlled", dataset="backup", status="degraded"))
        session.add(JobRun(id="backup-old-failure", job_name="controlled", business_key="old", status="degraded"))
        session.flush()
        session.add(SourcePayload(batch_id="backup-raw", api_name="controlled", request_hash="a" * 64, content_hash="b" * 64, payload={"value": "原始正文", "null": None}))
        session.add(JobFailure(job_run_id="backup-old-failure", dataset="controlled", request_key="old", error_code="source_failed", message="受控旧失败", retryable=True))
        session.add(JobCursor(job_name="controlled", cursor_key="history", cursor_value={"completed": ["2026-08"], "pending": ["2026-09"]}))
    with snapshot_inventory(engine, timeout=120) as view:
        return read_inventory(view.connection, timeout=120)


@contextmanager
def disposable_restore_target(options, *, created=None, before=None, after=None):
    owner = uuid.uuid4().hex
    with PostgresContainer("postgres:17-alpine", driver="psycopg").with_kwargs(labels={OWNER_LABEL: owner}) as postgres:
        target = parse_target(postgres.get_connection_url(driver="psycopg"))
        container = postgres.get_wrapped_container()
        if created is not None:
            created.append(container)
        def assert_owned():
            container.reload()
            assert container.labels[OWNER_LABEL] == owner
            assert container.id == postgres.get_wrapped_container().id
        if before is not None:
            before(target)
        try:
            yield SimpleNamespace(target=target, container_id=container.id, owner_token=owner, assert_owned=assert_owned)
        finally:
            if after is not None:
                after(target)
            assert_owned()
