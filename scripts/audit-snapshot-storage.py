"""测量当前发布与同版历史正文。只读源库，物理体积在一次性 PG17 核对。"""

import json
from pathlib import Path
import sys

from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.shared.snapshot_sql import PUBLISHED_SNAPSHOTS_SQL
from app.config import settings
from app.shared.readonly_database import open_readonly_factory

MATCH = f"""SELECT p.product,p.version,p.as_of,p.payload,
    h.id AS historical_id, (h.payload::jsonb = p.payload::jsonb) AS equal_json,
    (h.payload::text = p.payload::text) AS equal_text, h.as_of=p.as_of AS equal_as_of,
    pg_column_size(p.payload) AS current_column_bytes, pg_column_size(h.payload) AS history_column_bytes,
    octet_length(p.payload::text) AS text_bytes
    FROM {PUBLISHED_SNAPSHOTS_SQL} p LEFT JOIN mart.page_snapshot h
    ON h.product=p.product AND h.snapshot_version=p.version"""


def measure_pointer(source, target):
    # 目标为新建空表；历史表、其索引和正文不变，只比较两种当前指针的增量空间。
    with target.begin() as conn:
        conn.exec_driver_sql(
            """CREATE TABLE pointer_baseline(
          product varchar(128) PRIMARY KEY, version varchar(64) NOT NULL,as_of timestamptz NOT NULL,
          payload json NOT NULL,stale boolean NOT NULL,stale_reason text,
          freshness_checked_at timestamptz,published_at timestamptz NOT NULL);
          CREATE TABLE pointer_reference (LIKE pointer_baseline INCLUDING ALL);
          ALTER TABLE pointer_reference ALTER COLUMN payload DROP NOT NULL, ADD COLUMN history_id integer;
          ALTER TABLE pointer_reference ADD CHECK ((payload IS NULL) <> (history_id IS NULL));
          CREATE TABLE current_history(id integer PRIMARY KEY,payload json NOT NULL);
          ALTER TABLE pointer_reference ADD FOREIGN KEY(history_id) REFERENCES current_history(id);"""
        )
    d = target.raw_connection()
    try:
        with source.connection().connection.driver_connection.cursor() as src, d.cursor() as dst:
            for name, query in [
                (
                    "pointer_baseline",
                    f"SELECT product,version,as_of,payload,stale,stale_reason,freshness_checked_at,published_at FROM {PUBLISHED_SNAPSHOTS_SQL} published ORDER BY product",
                ),
                (
                    "current_history",
                    f"SELECT h.id,h.payload FROM mart.page_snapshot h JOIN {PUBLISHED_SNAPSHOTS_SQL} p ON h.product=p.product AND h.snapshot_version=p.version ORDER BY h.id",
                ),
                (
                    "pointer_reference",
                    f"""SELECT p.product,p.version,p.as_of,
                    CASE WHEN p.payload::text=h.payload::text THEN NULL ELSE p.payload END,
                    p.stale,p.stale_reason,p.freshness_checked_at,p.published_at,
                    CASE WHEN p.payload::text=h.payload::text THEN h.id ELSE NULL END
                    FROM {PUBLISHED_SNAPSHOTS_SQL} p LEFT JOIN mart.page_snapshot h
                    ON h.product=p.product AND h.snapshot_version=p.version ORDER BY p.product""",
                ),
            ]:
                with src.copy(f"COPY ({query}) TO STDOUT") as reader, dst.copy(
                    f"COPY {name} FROM STDIN"
                ) as writer:
                    for chunk in reader:
                        writer.write(chunk)
        d.commit()
    finally:
        d.close()
    with target.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in ["pointer_baseline", "pointer_reference"]:
            conn.exec_driver_sql("VACUUM FULL ANALYZE " + table)
        sizes = {
            t: conn.exec_driver_sql(
                f"SELECT pg_total_relation_size('{t}')"
            ).scalar_one()
            for t in ["pointer_baseline", "pointer_reference"]
        }
        comparison = (
            conn.exec_driver_sql(
                """SELECT count(*) AS pointers,
          count(*) FILTER(WHERE n.history_id IS NOT NULL) AS references,
          count(*) FILTER(WHERE b.payload::text IS DISTINCT FROM coalesce(n.payload,h.payload)::text OR
             (b.product,b.version,b.as_of,b.stale,b.stale_reason,b.freshness_checked_at,b.published_at)
               IS DISTINCT FROM (n.product,n.version,n.as_of,n.stale,n.stale_reason,n.freshness_checked_at,n.published_at)) AS conflicts
          FROM pointer_baseline b FULL JOIN pointer_reference n USING(product)
          LEFT JOIN current_history h ON h.id=n.history_id"""
            )
            .mappings()
            .one()
        )
        assert comparison["conflicts"] == 0
    # 历史回删和当前指针清理分阶段。单纯引用会提前影响当前读。
    with target.connect() as conn, conn.begin() as transaction:
        conn.exec_driver_sql(
            """
            UPDATE current_history SET payload='{"audit":"only historical row changed"}'::json
            WHERE id=(SELECT history_id FROM pointer_reference WHERE product='market-explorer')
        """
        )
        baseline_unchanged, alias_changed = conn.exec_driver_sql(
            """
            SELECT b.payload::text <> '{"audit":"only historical row changed"}',
                   coalesce(n.payload,h.payload)::text = '{"audit":"only historical row changed"}'
            FROM pointer_baseline b JOIN pointer_reference n USING(product)
            JOIN current_history h ON n.history_id=h.id WHERE n.product='market-explorer'
        """
        ).one()
        assert baseline_unchanged and alias_changed
        transaction.rollback()
    return {
        "historyOnlyEditChangesAliasedCurrent": alias_changed,
        "sameEditLeavesIndependentCurrentUnchanged": baseline_unchanged,
        "bytes": sizes,
        "netPointerBytesSaved": sizes["pointer_baseline"] - sizes["pointer_reference"],
        "resolvedReadComparison": dict(comparison),
    }


def main():
    with open_readonly_factory(
        settings.database_read_url
    ) as factory, factory() as source:
        assert source.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        rows = [
            dict(r)
            for r in source.execute(
                text(
                    "SELECT product,version,as_of::text,historical_id,equal_json,equal_text,equal_as_of,current_column_bytes,history_column_bytes,text_bytes FROM ("
                    + MATCH
                    + ") pairs ORDER BY product"
                )
            ).mappings()
        ]
        equivalent = [
            r for r in rows if r["equal_json"] and r["equal_text"] and r["equal_as_of"]
        ]
        others = [r for r in rows if r not in equivalent]
        report = {
            "current": len(rows),
            "equivalent": equivalent,
            "workbenchOverrides": sum(
                r["product"].startswith("stock-workbench:") for r in others
            ),
            "otherOverrides": [
                r["product"]
                for r in others
                if not r["product"].startswith("stock-workbench:")
            ],
            "unmatchedHistory": [
                r["product"] for r in rows if r["historical_id"] is None
            ],
        }
        report["historyMutationSurface"] = []
        inspect = text(
            """
            WITH bodies AS MATERIALIZED (
                SELECT payload::jsonb AS body FROM mart.page_snapshot WHERE id=ANY(:ids)
            )
            SELECT count(*) AS versions,
              count(*) FILTER (WHERE jsonb_exists(body,'marketNews')) AS news_field_versions,
              count(*) FILTER (WHERE jsonb_typeof(body->'marketNews')='array'
                AND jsonb_array_length(body->'marketNews')>0) AS nonempty_news_versions,
              count(*) FILTER (WHERE jsonb_typeof(body->'dailyChart'->'announcements')='array') AS announcement_versions
            FROM bodies
        """
        )
        for item in equivalent:
            product = item["product"]
            identifiers = source.scalars(
                text(
                    "SELECT id FROM mart.page_snapshot WHERE product=:product ORDER BY id"
                ),
                {"product": product},
            ).all()
            counts = {}
            for offset in range(0, len(identifiers), 8):
                group = (
                    source.execute(inspect, {"ids": identifiers[offset : offset + 8]})
                    .mappings()
                    .one()
                )
                for key, value in group.items():
                    counts[key] = counts.get(key, 0) + value
            report["historyMutationSurface"].append({"product": product, **counts})
        with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
            target = create_engine(pg.get_connection_url(driver="psycopg"))
            try:
                report["isolated"] = measure_pointer(source, target)
            finally:
                target.dispose()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
