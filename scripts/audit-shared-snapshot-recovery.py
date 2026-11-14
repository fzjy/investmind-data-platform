"""使用实际归档在仅限当前测试的 PG17 中验证转换、逻辑行与恢复。"""

from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app import config as settings_module
from app.config import Settings
from app.shared.snapshot_sql import PUBLISHED_SNAPSHOTS_SQL
from app.shared.repository import SnapshotRepository
from sqlalchemy.orm import sessionmaker

parser = ArgumentParser(description=__doc__)
parser.add_argument("archive", type=Path, help="已完成验证的旧版 database.dump")
parser.add_argument("--expected-sha256", required=True, help="归档清单中的文件摘要")
args = parser.parse_args()
backup = args.archive
with backup.open("rb") as file:
    from hashlib import file_digest

    sha = file_digest(file, "sha256").hexdigest()
if sha != args.expected_sha256.lower():
    raise ValueError("输入归档摘要不匹配")
cfg = Config(str(root / "backend/alembic.ini"))


def docker(pg, args, **kwargs):
    return subprocess.run(
        ["docker", "exec", "-i", pg.get_wrapped_container().id, *args],
        check=True,
        **kwargs
    )


def restore(pg, f, db):
    docker(
        pg,
        [
            "pg_restore",
            "-U",
            "test",
            "-d",
            db,
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--single-transaction",
        ],
        stdin=f,
    )


def proof(engine):
    with engine.begin() as c:
        rows = c.execute(
            text(
                "SELECT product,version,as_of,stale,stale_reason, freshness_checked_at, published_at, md5(payload::text) FROM "
                + PUBLISHED_SNAPSHOTS_SQL
                + " AS r ORDER BY product"
            )
        ).all()
        h = c.exec_driver_sql(
            "SELECT count(*), md5(string_agg(md5(row_to_json(r)::text),'' ORDER BY id)) FROM (SELECT id,product,snapshot_version,as_of,payload FROM mart.page_snapshot) r"
        ).one()
        return {
            "currentCount": len(rows),
            "currentSha256": sha256(repr(rows).encode()).hexdigest(),
            "historyCount": h[0],
            "historyHash": h[1],
        }


def size(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        for table in ("page_snapshot", "published_snapshot"):
            c.exec_driver_sql("VACUUM FULL ANALYZE mart." + table)
        return dict(
            c.exec_driver_sql(
                "select relname,pg_total_relation_size(oid) from pg_class where oid in ('mart.page_snapshot'::regclass,'mart.published_snapshot'::regclass)"
            ).all()
        )


with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
    url = pg.get_connection_url(driver="psycopg")
    with backup.open("rb") as f:
        restore(pg, f, "test")
    engine = create_engine(url)
    settings_module.settings = Settings(database_url=url)
    try:
        before = proof(engine)
        old_size = size(engine)
        with engine.connect() as c:
            old_revision = c.exec_driver_sql(
                "select version_num from alembic_version"
            ).scalar_one()
        if old_revision != "20261111_0020":
            raise ValueError("此对照仅适用于 0020 旧版归档")
        command.upgrade(cfg, "20261115_0021")
        assert proof(engine) == before
        with engine.connect() as c:
            shared = dict(
                c.exec_driver_sql(
                    "select product,history_generation from mart.published_snapshot where history_version is not null"
                ).all()
            )
            versions = dict(
                c.exec_driver_sql(
                    "select product,version from mart.published_snapshot where history_version is not null"
                ).all()
            )
            assert shared and shared.keys() <= {
                "screener",
                "portfolio",
                "market-overview",
                "macro-transmission",
                "macro-insights",
            }
        repo = SnapshotRepository(sessionmaker(engine))
        for prod, version in versions.items():
            assert repo.read(prod).raw == repo.read_versioned(prod, version).raw
        new_size = size(engine)
        # 在同一个受控容器内用归档流重新恢复到不同数据库，无生产连接。
        with TemporaryDirectory() as td:
            path = Path(td) / "new.dump"
            with path.open("wb") as dst:
                docker(pg, ["pg_dump", "-Fc", "-U", "test", "-d", "test"], stdout=dst)
            docker(pg, ["createdb", "-U", "test", "restored"])
            with path.open("rb") as f:
                restore(pg, f, "restored")
            restored = create_engine(engine.url.set(database="restored"))
            try:
                assert proof(restored) == before
                with restored.connect() as c:
                    assert (
                        dict(
                            c.exec_driver_sql(
                                "select product,history_generation from mart.published_snapshot where history_version is not null"
                            ).all()
                        )
                        == shared
                    )
            finally:
                restored.dispose()
            archive_bytes = path.stat().st_size
        command.downgrade(cfg, old_revision)
        assert proof(engine) == before
        with engine.connect() as c:
            assert (
                c.exec_driver_sql(
                    "select count(*) from mart.published_snapshot where json_typeof(payload)<>'object'"
                ).scalar_one()
                == 0
            )
        print(
            json.dumps(
                {
                    "oldArchiveSha256": sha,
                    "oldArchiveBytes": backup.stat().st_size,
                    "oldRevision": old_revision,
                    "newRevision": "20261115_0021",
                    "logical": before,
                    "sharedVersions": versions,
                    "compactBeforeBytes": old_size,
                    "compactAfterBytes": new_size,
                    "netBytesSaved": sum(old_size.values()) - sum(new_size.values()),
                    "newPgDumpBytes": archive_bytes,
                    "restoredAndDowngraded": True,
                },
                indent=2,
            )
        )
    finally:
        engine.dispose()
