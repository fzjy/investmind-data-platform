from __future__ import annotations

from contextlib import contextmanager
import re
from sqlalchemy import MetaData, Table, Text, cast, func, inspect, literal_column, select, text

SCHEMAS = ("raw", "core", "derived", "mart", "ops")
RUN_RELATIONS = {("ops", "job_failure"): "job_run_id", ("ops", "quality_check_result"): "job_run_id"}
BATCH_TABLES = {"source_payload", "rejected_record"}
PAGE = 25


@contextmanager
def reading(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
        yield conn


def list_tables(engine):
    with reading(engine) as conn:
        inspector = inspect(conn)
        schemas = set(inspector.get_schema_names())
        return [{"schema": schema, "name": name} for schema in SCHEMAS if schema in schemas
                for name in sorted(inspector.get_table_names(schema=schema))]


def _table(conn, schema, name):
    if schema not in SCHEMAS or name not in inspect(conn).get_table_names(schema=schema):
        raise KeyError(name)
    return Table(name, MetaData(), schema=schema, autoload_with=conn, resolve_fks=False)


def _row_token():
    # ctid 可复用，同时匹配 xmin。记录已变动时由界面重新加载。
    return literal_column("tableoid::text || '/' || ctid::text || '/' || xmin::text")


def preview(engine, schema, name, offset=0, run=None, batch=None):
    with reading(engine) as conn:
        table = _table(conn, schema, name)
        columns = list(table.columns)
        size = max(64, min(1024, 32000 // max(len(columns), 1)))
        projections = [func.left(cast(c, Text), size + 1) for c in columns]
        keys = list(table.primary_key.columns)
        if not keys:
            for constraint in inspect(conn).get_unique_constraints(name, schema=schema):
                candidate = [table.c[k] for k in constraint["column_names"]]
                if candidate and all(not c.nullable for c in candidate):
                    keys = candidate
                    break
        query = select(*projections, _row_token()).select_from(table)
        if run is not None:
            field = RUN_RELATIONS.get((schema, name))
            if not field:
                raise ValueError("该表无此运行关联")
            query = query.where(table.c[field] == run)
        if batch is not None:
            if schema != "raw" or name not in BATCH_TABLES:
                raise ValueError("该表无此批次关联")
            query = query.where(table.c.batch_id == batch)
        order = [c.asc() for c in keys]
        date_fields = {"trade_date", "business_date", "started_at", "captured_at", "fetched_at"}
        indexes = inspect(conn).get_indexes(name, schema=schema)
        indexed = {i["column_names"][0] for i in indexes if i.get("column_names")}
        if keys:
            indexed.add(keys[0].name)
        date_key = next((c for c in columns if c.name in indexed & date_fields), None)
        if date_key is not None:
            order = [date_key.desc().nulls_last(), *(c.asc() for c in keys if c.name != date_key.name)]
        # 作业表以有索引的业务键为准；无键表只保证本次读取顺序。
        records = conn.execute(query.order_by(*order).offset(offset).limit(PAGE + 1)).all()
        rows = []
        for row in records[:PAGE]:
            values = row[:-1]
            rows.append({"id": row[-1], "values": [v[:size] if v is not None else None for v in values],
                         "more": [v is not None and len(v) > size for v in values]})
        return {"columns": [{"name": c.name, "type": str(c.type), "comment": c.comment} for c in columns],
                "rows": rows, "nextCursor": offset + PAGE if len(records) > PAGE else None,
                "previousCursor": max(0, offset - PAGE) if offset else None,
                "orderHint": ("按 " + date_key.name + " 倒序；" if date_key is not None else "") +
                ("唯一键 " + "、".join(c.name for c in keys) if keys else "无唯一键，翻页顺序可能变化")}


def cell(engine, schema, name, column, row, start):
    token = re.fullmatch(r"(\d{1,10})/(\(\d{1,10},\d{1,5}\))/(\d{1,10})", row)
    if not token:
        raise KeyError("记录标识无效")
    with reading(engine) as conn:
        table = _table(conn, schema, name)
        if column not in table.c:
            raise KeyError(column)
        oid, tid, version = token.groups()
        position = text("ctid = CAST(:tid AS tid) AND tableoid = CAST(:oid AS oid) AND xmin::text = :version")
        value = conn.execute(select(func.substr(cast(table.c[column], Text), start + 1, 8193)).where(position),
                             dict(tid=tid, oid=oid, version=version)).scalar_one_or_none()
        if value is None:
            raise KeyError(row)
        return {"value": value[:8192], "next": start + 8192 if len(value) > 8192 else None}
