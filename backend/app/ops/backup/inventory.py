from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
import time
from typing import Iterator

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.ops.backup.contracts import BackupError, DatabaseInventory, PublicationInventory, SequenceInventory, TableInventory
from app.ops.backup.budget import remaining_timeout
from app.ops.backup.project_guards import USER_NAMESPACE, ProjectGuardDefinition, inspect_project_guards
from app.ops.backup.constraint_compatibility import COMPATIBLE_TABLES, canonical_restored_check


@dataclass(frozen=True)
class CatalogView:
    relations: tuple[Mapping[str, object], ...]
    guards_by_table: Mapping[tuple[str, str], tuple[ProjectGuardDefinition, ...]]


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(schema: str, name: str) -> str:
    return quote_identifier(schema) + "." + quote_identifier(name)


class Budget:
    def __init__(self, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout

    def remaining(self) -> float:
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise BackupError("backup_budget_exhausted", 3, "timeout")
        return remaining_timeout(value)

    def execute(self, connection: Connection, sql: str, parameters: Mapping[str, object] | None = None, *, stream: bool = False):
        milliseconds = max(1, int(self.remaining() * 1000))
        connection.execute(text("SELECT set_config('statement_timeout', :value, true)"), {"value": str(milliseconds)})
        statement = text(sql)
        if stream:
            statement = statement.execution_options(stream_results=True, max_row_buffer=1000)
        return connection.execute(statement, parameters or {})


def _canonical(value: object) -> bytes:
    # 类型标签 + 长度前缀：不会混淆 JSON 数字/字符串、null/正文或行边界。
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, Decimal):
        return b"d" + str(value).encode("ascii") + b";"
    if isinstance(value, str):
        content = value.encode("utf-8")
        return b"s" + len(content).to_bytes(8, "big") + content
    if isinstance(value, list):
        return b"[" + b"".join(_canonical(item) for item in value) + b"]"
    if isinstance(value, dict):
        return b"{" + b"".join(_canonical(key) + _canonical(value[key]) for key in sorted(value)) + b"}"
    raise BackupError("invalid_inventory_value", 4, "inventory")


def fingerprint_rows(rows: Iterable[str], *, check: Callable[[], object] | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    iterator = iter(rows)
    while True:
        if check is not None:
            check()
        try:
            row = next(iterator)
        except StopIteration:
            break
        if check is not None:
            check()
        encoded = _canonical(json.loads(row, parse_int=Decimal, parse_float=Decimal))
        if check is not None:
            check()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def validate_catalog(relations: Iterable[Mapping[str, object]], problems: set[str]) -> None:
    if problems:
        raise BackupError("unsupported_database_objects", 2, "inventory")
    for relation in relations:
        if relation["kind"] not in {"r", "S", "i"} or relation["persistence"] != "p" or relation["tablespace"] != 0:
            raise BackupError("unsupported_database_objects", 2, "inventory")


def _catalog(connection: Connection, budget: Budget) -> CatalogView:
    relations = tuple(budget.execute(connection, f"""
        SELECT n.nspname AS schema_name, c.relname AS name, c.oid,
               c.relkind AS kind, c.relpersistence AS persistence, c.reltablespace AS tablespace
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {USER_NAMESPACE} ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """).mappings())
    problems = set(budget.execute(connection, f"""
        SELECT 'rule' WHERE EXISTS (SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {USER_NAMESPACE})
        UNION ALL SELECT 'inheritance' WHERE EXISTS (SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {USER_NAMESPACE})
        UNION ALL SELECT 'large_object' WHERE EXISTS (SELECT 1 FROM pg_largeobject_metadata)
        UNION ALL SELECT 'extension' WHERE EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
        UNION ALL SELECT 'custom_type' WHERE EXISTS (
            SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace LEFT JOIN pg_class c ON c.oid=t.typrelid
            WHERE {USER_NAMESPACE} AND t.typcategory <> 'A' AND (t.typtype <> 'c' OR c.relkind IS DISTINCT FROM 'r'))
        UNION ALL SELECT 'unvalidated_constraint' WHERE EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE {USER_NAMESPACE} AND NOT c.convalidated)
        UNION ALL SELECT 'invalid_index' WHERE EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {USER_NAMESPACE} AND (NOT i.indisvalid OR NOT i.indisready))
        UNION ALL SELECT 'row_security' WHERE EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {USER_NAMESPACE} AND (c.relrowsecurity OR c.relforcerowsecurity))
        UNION ALL SELECT 'tablespace' WHERE EXISTS (SELECT 1 FROM pg_database d JOIN pg_tablespace t ON t.oid=d.dattablespace WHERE d.datname=current_database() AND t.spcname <> 'pg_default')
        UNION ALL SELECT 'custom_collation' WHERE EXISTS (SELECT 1 FROM pg_collation c JOIN pg_namespace n ON n.oid=c.collnamespace WHERE {USER_NAMESPACE})
    """).scalars())
    validate_catalog(relations, problems)
    guards = inspect_project_guards(connection, budget, relations)
    budget.remaining()
    return CatalogView(relations, guards)


@dataclass(frozen=True)
class SnapshotView:
    connection: Connection
    snapshot_id: str
    server_major: int


@contextmanager
def snapshot_inventory(engine: Engine, *, timeout: float) -> Iterator[SnapshotView]:
    budget = Budget(timeout)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                connection.execute(text("SET LOCAL search_path TO pg_catalog"))
                catalog = _catalog(connection, budget)
                for relation in catalog.relations:
                    if relation["kind"] == "r":
                        name = _qualified(str(relation["schema_name"]), str(relation["name"]))
                        budget.execute(connection, f"LOCK TABLE {name} IN ACCESS SHARE MODE")
                major = int(budget.execute(connection, "SHOW server_version_num").scalar_one()) // 10000
                snapshot = str(budget.execute(connection, "SELECT pg_export_snapshot()").scalar_one())
                yield SnapshotView(connection, snapshot, major)
    except SQLAlchemyError:
        raise BackupError("source_snapshot_failed", 3, "snapshot") from None


def _structure(connection: Connection, relation: Mapping[str, object], budget: Budget, *, guards: tuple[ProjectGuardDefinition, ...] = (), restore_compatibility: bool = False) -> str:
    params = {"oid": relation["oid"]}
    columns = [dict(row) for row in budget.execute(connection, """
        SELECT a.attnum, a.attname, format_type(a.atttypid,a.atttypmod) AS type,
               a.attnotnull, a.attidentity, a.attgenerated,
               pg_get_expr(d.adbin,d.adrelid) AS default_expression,
               cn.nspname AS collation_schema, col.collname AS collation_name
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        LEFT JOIN pg_collation col ON col.oid=a.attcollation LEFT JOIN pg_namespace cn ON cn.oid=col.collnamespace
        WHERE a.attrelid=:oid AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
    """, params).mappings()]
    # pg_dump 恢复的是逻辑列顺序，不保留 DROP COLUMN 留下的物理 attnum 空槽。
    for position, column in enumerate(columns, start=1):
        column["attnum"] = position
    constraints = [dict(row) for row in budget.execute(connection, """
        SELECT conname, contype, condeferrable, condeferred, convalidated, pg_get_constraintdef(oid,true) AS definition
        FROM pg_constraint WHERE conrelid=:oid ORDER BY conname COLLATE "C"
    """, params).mappings()]
    if restore_compatibility:
        for constraint in constraints:
            constraint["definition"] = canonical_restored_check(str(relation["schema_name"]), str(relation["name"]), constraint["conname"], constraint["definition"])
    indexes = [dict(row) for row in budget.execute(connection, """
        SELECT c.relname, pg_get_indexdef(i.indexrelid) AS definition, i.indisvalid, i.indisready,
               i.indisclustered, i.indisreplident
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indrelid=:oid ORDER BY c.relname COLLATE "C"
    """, params).mappings()]
    structure = {"columns": columns, "constraints": constraints, "indexes": indexes}
    if guards:
        structure["projectImmutableGuards"] = [asdict(guard) for guard in guards]
    encoded = json.dumps(structure, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _sequences(connection: Connection, relations: tuple[Mapping[str, object], ...], budget: Budget) -> tuple[SequenceInventory, ...]:
    result: list[SequenceInventory] = []
    for relation in relations:
        if relation["kind"] != "S":
            continue
        rows = list(budget.execute(connection, """
            SELECT tn.nspname AS table_schema, t.relname AS table_name, a.attname AS column_name,
                   s.seqtypid::regtype::text AS type, s.seqstart, s.seqincrement, s.seqmax, s.seqmin, s.seqcache, s.seqcycle
            FROM pg_sequence s JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=s.seqrelid
                AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
            JOIN pg_class t ON t.oid=d.refobjid JOIN pg_namespace tn ON tn.oid=t.relnamespace
            JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
            WHERE s.seqrelid=:oid
        """, {"oid": relation["oid"]}).mappings())
        if len(rows) != 1 or rows[0]["seqincrement"] <= 0 or rows[0]["seqcycle"]:
            raise BackupError("unsupported_sequence", 2, "inventory")
        row = dict(rows[0])
        qualified = _qualified(row["table_schema"], row["table_name"])
        maximum = budget.execute(connection, f"SELECT max({quote_identifier(row['column_name'])}) FROM {qualified}").scalar_one()
        definition = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result.append(SequenceInventory(schema_name=str(relation["schema_name"]), sequence_name=str(relation["name"]),
            table_schema=row["table_schema"], table_name=row["table_name"], column_name=row["column_name"],
            maximum_key=maximum, increment=row["seqincrement"], definition_sha256=definition))
    return tuple(result)


def read_inventory(connection: Connection, *, timeout: float) -> DatabaseInventory:
    budget = Budget(timeout)
    try:
        connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        connection.execute(text("SET LOCAL search_path TO pg_catalog"))
        catalog = _catalog(connection, budget)
        relations = catalog.relations
        tables: list[TableInventory] = []
        for relation in relations:
            if relation["kind"] != "r":
                continue
            qualified = _qualified(str(relation["schema_name"]), str(relation["name"]))
            rows = budget.execute(connection, f'SELECT to_jsonb(t)::text AS row_data FROM {qualified} t ORDER BY to_jsonb(t)::text COLLATE "C"', stream=True)
            try:
                count, digest = fingerprint_rows(rows.scalars(), check=budget.remaining)
            finally:
                rows.close()
            table_key = (str(relation["schema_name"]), str(relation["name"]))
            tables.append(TableInventory(schema_name=table_key[0], table_name=table_key[1], row_count=count, rows_sha256=digest,
                schema_sha256=_structure(connection, relation, budget, guards=catalog.guards_by_table.get(table_key, ()))))
            budget.remaining()
        names = {(table.schema_name, table.table_name) for table in tables}
        versions = tuple(budget.execute(connection, "SELECT version_num FROM public.alembic_version ORDER BY version_num").scalars()) if ("public", "alembic_version") in names else ()
        publications: tuple[PublicationInventory, ...] = ()
        if ("mart", "published_snapshot") in names:
            values = budget.execute(connection, """
                SELECT product, version, stale,
                    CASE WHEN jsonb_typeof(payload::jsonb->'stocks')='array' THEN jsonb_array_length(payload::jsonb->'stocks') ELSE NULL END AS row_count
                FROM mart.published_snapshot ORDER BY product COLLATE "C"
            """).mappings()
            publications = tuple(PublicationInventory(**dict(value)) for value in values)
        sequences = _sequences(connection, relations, budget)
        budget.remaining()
        schemas = tuple(budget.execute(connection, f'SELECT n.nspname FROM pg_namespace n WHERE {USER_NAMESPACE} ORDER BY n.nspname COLLATE "C"').scalars())
        return DatabaseInventory(tables=tuple(tables), schemas=schemas, alembic_versions=versions, publications=publications, sequences=sequences)
    except SQLAlchemyError:
        raise BackupError("database_inventory_failed", 3, "inventory") from None


def check_sequences(connection: Connection, inventory: DatabaseInventory) -> None:
    budget = Budget(30)
    for sequence in inventory.sequences:
        row = budget.execute(connection, f"SELECT last_value, is_called FROM {_qualified(sequence.schema_name, sequence.sequence_name)}").mappings().one()
        next_value = row["last_value"] + (sequence.increment if row["is_called"] else 0)
        maximum = budget.execute(connection, f"SELECT seqmax FROM pg_sequence WHERE seqrelid=CAST(:name AS regclass)", {"name": _qualified(sequence.schema_name, sequence.sequence_name)}).scalar_one()
        if next_value > maximum or (sequence.maximum_key is not None and next_value <= sequence.maximum_key):
            raise BackupError("unsafe_restored_sequence", 4, "verification")


def assert_restored_inventory(connection: Connection, actual: DatabaseInventory, expected: DatabaseInventory, *, timeout: float) -> None:
    """原哈希优先；仅固定CHECK的等价重写可以生成第二个受核验哈希。"""
    if actual == expected:
        return
    def mismatch() -> None:
        raise BackupError("restored_inventory_mismatch", 4, "verification")
    expected_tables = {(t.schema_name, t.table_name): t for t in expected.tables}
    # 先绑定全部行/表/顺序/版本/指针/序列，不能以CHECK兼容遮盖其他差异。
    bound = tuple(t.model_copy(update={"schema_sha256":expected_tables[(t.schema_name,t.table_name)].schema_sha256})
        if (t.schema_name,t.table_name) in expected_tables else t for t in actual.tables)
    if actual.model_copy(update={"tables":bound}) != expected:
        mismatch()
    changed = {(t.schema_name,t.table_name) for t in actual.tables if t.schema_sha256 != expected_tables[(t.schema_name,t.table_name)].schema_sha256}
    if not changed <= COMPATIBLE_TABLES:
        mismatch()
    budget = Budget(timeout)
    catalog = _catalog(connection, budget)  # 同一严格保护检查，不借兼容模式忽略未知对象。
    relations = {(str(r["schema_name"]),str(r["name"])):r for r in catalog.relations if r["kind"] == "r"}
    for key in sorted(changed):
        if key not in relations:
            mismatch()
        digest = _structure(connection, relations[key], budget, guards=catalog.guards_by_table.get(key, ()), restore_compatibility=True)
        if digest != expected_tables[key].schema_sha256:
            mismatch()
        budget.remaining()
