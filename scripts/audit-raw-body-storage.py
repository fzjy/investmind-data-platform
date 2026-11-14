"""在一次性 PostgreSQL 17 对照两种原件结构与备份兼容性。源事务只读。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from testcontainers.community.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings
from app.db.models import SourcePayload
from app.shared.readonly_database import open_readonly_factory
from app.ops.backup.contracts import BackupError
from app.ops.backup.inventory import snapshot_inventory

SCHEMA = """
CREATE SCHEMA raw;
CREATE TABLE raw.ingestion_batch(id varchar(36) PRIMARY KEY,source varchar(64) NOT NULL);
CREATE TABLE raw.source_payload(
 id serial PRIMARY KEY,batch_id varchar(36) NOT NULL REFERENCES raw.ingestion_batch(id) ON DELETE CASCADE,
 api_name varchar(128) NOT NULL,request_hash varchar(64) NOT NULL,content_hash varchar(64) NOT NULL,
 payload json NOT NULL,captured_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(batch_id,request_hash));
CREATE TABLE raw.quote_lineage(id integer PRIMARY KEY,source_payload_id integer NOT NULL REFERENCES raw.source_payload(id));
"""
SPLIT = """
ALTER TABLE raw.source_payload RENAME TO source_capture;
CREATE TABLE raw.source_body(digest bytea PRIMARY KEY,payload json NOT NULL);
ALTER TABLE raw.source_capture ADD body_id bytea;
INSERT INTO raw.source_body(digest,payload)
 SELECT DISTINCT ON (digest) digest,payload FROM
 (SELECT sha256(convert_to(payload::text,'UTF8')) AS digest,payload FROM raw.source_capture) contents
 ORDER BY digest;
UPDATE raw.source_capture SET body_id=sha256(convert_to(payload::text,'UTF8'));
ALTER TABLE raw.source_capture ALTER COLUMN body_id SET NOT NULL,
 ADD CONSTRAINT capture_body_fk FOREIGN KEY (body_id) REFERENCES raw.source_body(digest);
CREATE INDEX capture_by_body ON raw.source_capture(body_id);
CREATE VIEW raw.source_payload AS
 SELECT c.id,c.batch_id,c.api_name,c.request_hash,c.content_hash,b.payload,c.captured_at
 FROM raw.source_capture c JOIN raw.source_body b ON c.body_id=b.digest;
"""
# 样本试验仅在隔离库执行。原件身份仍由捕获表/原序列分配。
WRITE = """
CREATE FUNCTION raw.write_source_payload() RETURNS trigger LANGUAGE plpgsql AS $func$
DECLARE key bytea;
BEGIN
 IF TG_OP='DELETE' THEN
  DELETE FROM raw.source_capture WHERE id=OLD.id;
  RETURN OLD;
 END IF;
 key=sha256(convert_to(NEW.payload::text,'UTF8'));
 INSERT INTO raw.source_body(digest,payload) VALUES(key,NEW.payload)
 ON CONFLICT(digest) DO NOTHING;
 IF (SELECT payload::text FROM raw.source_body WHERE digest=key) IS DISTINCT FROM NEW.payload::text THEN
  RAISE EXCEPTION 'original bytes disagree';
 END IF;
 IF TG_OP='UPDATE' THEN
  UPDATE raw.source_capture SET id=NEW.id,batch_id=NEW.batch_id,api_name=NEW.api_name,
   request_hash=NEW.request_hash,content_hash=NEW.content_hash,captured_at=NEW.captured_at,body_id=key
   WHERE id=OLD.id;
 ELSE
  INSERT INTO raw.source_capture(id,batch_id,api_name,request_hash,content_hash,captured_at,body_id)
   VALUES(COALESCE(NEW.id,nextval('raw.source_payload_id_seq')),NEW.batch_id,NEW.api_name,
    NEW.request_hash,NEW.content_hash,NEW.captured_at,key)
   RETURNING id INTO NEW.id;
 END IF;
 RETURN NEW;
END $func$;
ALTER VIEW raw.source_payload ALTER COLUMN captured_at SET DEFAULT now();
CREATE TRIGGER capture_write INSTEAD OF INSERT OR UPDATE OR DELETE ON raw.source_payload
 FOR EACH ROW EXECUTE FUNCTION raw.write_source_payload();
"""

VERSION_COUNTS = """
WITH grouped AS (
 SELECT b.source,p.api_name,p.request_hash,p.content_hash,count(*) AS captures
 FROM raw.source_payload p JOIN raw.ingestion_batch b ON b.id=p.batch_id GROUP BY 1,2,3,4
), requests AS (SELECT source,api_name,request_hash,count(*) AS signatures FROM grouped GROUP BY 1,2,3)
SELECT (SELECT sum(captures-1)::integer FROM grouped), (SELECT count(*) FROM requests WHERE signatures>1)
"""


def query(conn, statement):
    return conn.execute(text(statement))


def sizes(target, names):
    with target.connect() as conn:
        return {
            name: query(
                conn, f"SELECT pg_total_relation_size('raw.{name}')"
            ).scalar_one()
            for name in names
        }


def snapshot(conn):
    hashes = query(
        conn,
        """SELECT count(*), encode(sha256(convert_to(
      string_agg(encode(sha256(convert_to(row_to_json(p)::text,'UTF8')),'hex'),'' ORDER BY id),'UTF8')),'hex')
      FROM (SELECT id,batch_id,api_name,request_hash,content_hash,payload,captured_at FROM raw.source_payload) p""",
    ).one()
    return {
        "captures": hashes[0],
        "allRowsSha256": hashes[1],
        "repeatCapturesAndRevisedRequests": list(query(conn, VERSION_COUNTS).one()),
        "quoteReferences": query(
            conn,
            "SELECT count(*) FROM raw.quote_lineage q JOIN raw.source_payload p ON p.id=q.source_payload_id",
        ).scalar_one(),
    }


def vacuum(target, name):
    with target.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM FULL ANALYZE raw." + name)


def run(source, target):
    raw_target = target.raw_connection()
    try:
        with source.connection().connection.driver_connection.cursor() as origin, raw_target.cursor() as dest:
            dest.execute(SCHEMA)
            for table, sql in [
                ("ingestion_batch", "SELECT id,source FROM raw.ingestion_batch"),
                (
                    "source_payload",
                    "SELECT id,batch_id,api_name,request_hash,content_hash,payload,captured_at FROM raw.source_payload ORDER BY id",
                ),
                (
                    "quote_lineage",
                    "SELECT id,source_payload_id FROM core.market_daily_quote",
                ),
            ]:
                with origin.copy(f"COPY ({sql}) TO STDOUT") as reader, dest.copy(
                    f"COPY raw.{table} FROM STDIN"
                ) as writer:
                    for chunk in reader:
                        writer.write(chunk)
            dest.execute(
                "SELECT setval('raw.source_payload_id_seq',max(id)) FROM raw.source_payload"
            )
        raw_target.commit()
    finally:
        raw_target.close()
    vacuum(target, "source_payload")
    before = sizes(target, ["source_payload"])
    with target.begin() as conn:
        baseline = snapshot(conn)
        conn.exec_driver_sql(SPLIT)
        conflicting = query(
            conn,
            """SELECT count(*) FROM raw.source_capture c JOIN raw.source_body b ON c.body_id=b.digest
                                WHERE convert_to(c.payload::text,'UTF8')<>convert_to(b.payload::text,'UTF8')""",
        ).scalar_one()
        assert conflicting == 0
        conn.exec_driver_sql("ALTER TABLE raw.source_capture DROP COLUMN payload")
        conn.exec_driver_sql(WRITE)
        parity = snapshot(conn)
        assert parity == baseline
    for table in ["source_capture", "source_body"]:
        vacuum(target, table)
    after = sizes(target, ["source_capture", "source_body"])
    with target.begin() as conn:

        counts = query(conn, "SELECT count(*) FROM raw.source_body").scalar_one()
        for key in ("one", "two"):
            conn.execute(
                text("INSERT INTO raw.ingestion_batch VALUES (:id,'audit')"),
                {"id": "sample-" + key},
            )

    def capture(key):
        with target.begin() as conn:
            return conn.execute(
                text(
                    """INSERT INTO raw.source_payload(batch_id,api_name,request_hash,content_hash,payload)
               VALUES(:id,'audit','same','supplier',CAST(:body AS json)) RETURNING id"""
                ),
                {
                    "id": "sample-" + key,
                    "body": '{"sample":"two batches simultaneous"}',
                },
            ).scalar_one()

    with ThreadPoolExecutor(2) as pool:
        new_ids = list(pool.map(capture, ["one", "two"]))
    assert len(set(new_ids)) == 2
    with target.begin() as conn:
        assert (
            query(conn, "SELECT count(*) FROM raw.source_body").scalar_one()
            == counts + 1
        )
        conn.execute(
            text(
                "UPDATE raw.source_payload SET payload=CAST(:body AS json),content_hash='revised' WHERE id=:id"
            ),
            {"id": new_ids[0], "body": '{"sample":"separate revision"}'},
        )
        assert (
            query(
                conn,
                "SELECT count(DISTINCT payload::text) FROM raw.source_payload WHERE api_name='audit'",
            ).scalar_one()
            == 2
        )
    try:
        capture("one")
    except IntegrityError as exc:
        if getattr(getattr(exc, "orig", None), "sqlstate", None) != "23505":
            raise
    else:
        raise AssertionError("duplicate request within batch accepted")
    with Session(target) as session:
        saved = session.get(SourcePayload, new_ids[0])
        assert saved.payload == {"sample": "separate revision"}
        assert session.scalar(
            select(SourcePayload.payload).where(SourcePayload.id == new_ids[1])
        ) == {"sample": "two batches simultaneous"}
        saved.payload = {"sample": "ORM amendment"}
        saved.content_hash = "orm-revision"
        session.flush()
        session.refresh(saved)
        assert saved.payload == {"sample": "ORM amendment"}
        session.rollback()
    with Session(target) as session:
        record = SourcePayload(
            batch_id="sample-one",
            api_name="audit",
            request_hash="orm",
            content_hash="orm",
            payload={"probe": 1},
        )
        session.add(record)
        session.flush()
        assert record.id is not None and record.captured_at is not None
        session.rollback()
    with target.begin() as conn:
        conn.execute(
            text("DELETE FROM raw.source_payload WHERE id=:id"), {"id": new_ids[0]}
        )
        conn.execute(text("DELETE FROM raw.ingestion_batch WHERE source='audit'"))
        assert snapshot(conn) == baseline
        orphan_count = query(
            conn,
            """SELECT count(*) FROM raw.source_body b
                     WHERE NOT EXISTS (SELECT 1 FROM raw.source_capture c WHERE c.body_id=b.digest)""",
        ).scalar_one()
        assert orphan_count == 2
    try:
        with snapshot_inventory(target, timeout=30):
            pass
    except BackupError as exc:
        backup_status = exc.reason_code
        assert backup_status == "unsupported_database_objects"
    else:
        raise AssertionError("unexpected backup support")
    with target.begin() as conn:
        conn.exec_driver_sql(
            """DROP VIEW raw.source_payload; DROP FUNCTION raw.write_source_payload();
            ALTER TABLE raw.source_capture ADD COLUMN payload json;
            UPDATE raw.source_capture c SET payload=b.payload FROM raw.source_body b WHERE b.digest=c.body_id;
            ALTER TABLE raw.source_capture ALTER COLUMN payload SET NOT NULL, DROP COLUMN body_id;
            DROP TABLE raw.source_body; ALTER TABLE raw.source_capture RENAME TO source_payload"""
        )
        assert snapshot(conn) == baseline
    return {
        "before": baseline,
        "compactBaselineBytes": before,
        "splitTableBytes": after,
        "byteReduction": sum(before.values()) - sum(after.values()),
        "uniqueBodies": counts,
        "unchangedAfterMigration": True,
        "unchangedAfterRollback": True,
        "ormProjectionInsertAndUpdate": True,
        "concurrentSameBodyCaptureIds": new_ids,
        "duplicateWithinBatchSqlstate": "23505",
        "afterTwoTestCaptureRemovalsOrphanBodies": orphan_count,
        "backupInventory": backup_status,
    }


def main():
    with open_readonly_factory(
        settings.database_read_url
    ) as factory, factory() as source:
        assert source.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        measurements = dict(
            source.execute(
                text(
                    """
            WITH captured AS (
              SELECT sha256(convert_to(payload::text, 'UTF8')) AS digest,
                     octet_length(payload::text) AS n, pg_column_size(payload) AS stored
              FROM raw.source_payload
            ), bodies AS (
              SELECT digest, min(n) AS n, sum(stored) AS stored, max(stored) AS largest, count(*) AS captures
              FROM captured GROUP BY digest
            )
            SELECT sum(n*(captures-1))::bigint AS duplicate_text_bytes,
                   sum(stored-largest)::bigint AS duplicate_column_bytes,
                   sum(stored)::bigint AS all_stored_column_bytes,
                   pg_total_relation_size('raw.source_payload') AS live_relation_bytes
            FROM bodies
        """
                )
            )
            .mappings()
            .one()
        )
        with PostgresContainer("postgres:17-alpine", driver="psycopg") as container:
            engine = create_engine(container.get_connection_url(driver="psycopg"))
            try:
                result = run(source, engine)
                result["readonlySourceMeasurement"] = measurements
            finally:
                engine.dispose()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
