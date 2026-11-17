"""在独立只读事务中核对隔离库的关系、数据行版本及序列状态。"""
from sqlalchemy import Engine, text


def database_snapshot(engine: Engine) -> tuple:
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        schemas = connection.execute(text("""
            SELECT oid, nspname FROM pg_namespace
            WHERE NOT starts_with(nspname, 'pg_') AND nspname <> 'information_schema'
            ORDER BY nspname
        """)).all()
        relations = connection.execute(text("""
            SELECT n.nspname, c.relname, c.relkind, c.oid,
                   format('%I.%I', n.nspname, c.relname) AS qualified
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT starts_with(n.nspname, 'pg_') AND n.nspname <> 'information_schema'
            ORDER BY n.nspname, c.relname
        """)).all()
        definitions = connection.execute(text("""
            SELECT a.attrelid, a.attnum, a.xmin::text
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT starts_with(n.nspname, 'pg_') AND n.nspname <> 'information_schema'
            ORDER BY a.attrelid, a.attnum
        """)).all()
        versions = {}
        for relation in relations:
            if relation.relkind == "r":
                # MVCC 行版本随任一 UPDATE 改变，ctid 区分同一事务内的行。
                versions[relation.qualified] = connection.exec_driver_sql(
                    f"SELECT t.xmin::text, t.ctid::text, to_jsonb(t)::text FROM {relation.qualified} t ORDER BY t.ctid"
                ).all()
            elif relation.relkind == "S":
                versions[relation.qualified] = connection.exec_driver_sql(
                    f"SELECT last_value, is_called FROM {relation.qualified}"
                ).one()
        return schemas, relations, definitions, versions
