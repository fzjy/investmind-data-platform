from __future__ import annotations

import importlib
import importlib.util
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json

import pytest

from app.ops.backup.contracts import BackupError


def module():
    assert importlib.util.find_spec("app.ops.backup.inventory") is not None, "snapshot inventory is missing"
    return importlib.import_module("app.ops.backup.inventory")


def test_manifest_changes_when_same_row_count_changes_value() -> None:
    inventory = module()
    original = inventory.fingerprint_rows(iter(['{"amount":1.2300,"payload":{"a":1,"b":2},"date":"2020-01-01","optional":null}']))
    equivalent = inventory.fingerprint_rows(iter(['{"date":"2020-01-01","optional":null,"payload":{"b":2,"a":1},"amount":1.2300}']))
    changed = inventory.fingerprint_rows(iter(['{"amount":1.2301,"payload":{"a":1,"b":2},"date":"2020-01-01","optional":null}']))
    text_changed = inventory.fingerprint_rows(iter(['{"amount":1.2300,"payload":{"a":1,"b":2},"date":"2020-01-02","optional":null}']))
    assert original == equivalent
    assert original[0] == changed[0] == 1
    assert original[1] != changed[1]
    assert original[1] != text_changed[1]
    assert inventory.fingerprint_rows(iter(())) == (0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


def test_duplicate_rows_and_typed_values_do_not_collide() -> None:
    inventory = module()
    assert inventory.fingerprint_rows(iter(['{"v":1}', '{"v":1}']))[0] == 2
    assert inventory.fingerprint_rows(iter(['{"v":1}']))[1] != inventory.fingerprint_rows(iter(['{"v":"1"}']))[1]
    assert inventory.fingerprint_rows(iter(['{"v":null}']))[1] != inventory.fingerprint_rows(iter(['{"v":"null"}']))[1]
    assert inventory.fingerprint_rows(iter(['{"v":"ab"}', '{"v":"c"}']))[1] != inventory.fingerprint_rows(iter(['{"v":"a"}', '{"v":"bc"}']))[1]


@pytest.mark.parametrize("problem", ["foreign_table", "unlogged_table", "partition_table", "view", "materialized_view", "function", "trigger", "large_object", "tablespace", "extension", "custom_type", "unvalidated_constraint", "row_security"])
def test_unverifiable_objects_fail_closed(problem: str) -> None:
    inventory = module()
    with pytest.raises(BackupError) as caught:
        inventory.validate_catalog([], {problem})
    assert caught.value.exit_code == 2


@pytest.mark.parametrize(("kind", "persistence", "tablespace"), [("f", "p", 0), ("p", "p", 0), ("v", "p", 0), ("m", "p", 0), ("r", "u", 0), ("r", "p", 123)])
def test_relation_properties_are_validated(kind: str, persistence: str, tablespace: int) -> None:
    inventory = module()
    with pytest.raises(BackupError):
        inventory.validate_catalog([{"kind": kind, "persistence": persistence, "tablespace": tablespace}], set())


def test_regular_table_and_serial_sequence_are_supported() -> None:
    module().validate_catalog([
        {"kind": "r", "persistence": "p", "tablespace": 0},
        {"kind": "S", "persistence": "p", "tablespace": 0},
        {"kind": "i", "persistence": "p", "tablespace": 0},
    ], set())


class Rows:
    def __init__(self, rows=()): self.rows = rows
    def mappings(self): return iter(self.rows)
    def scalars(self): return iter(self.rows)


class CatalogConnection:
    def __init__(self, functions=(), triggers=(), relations=(), versions=("20261021_0015",)):
        self.functions, self.triggers, self.relations, self.versions = functions, triggers, relations, versions
        self.exported = False
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def connect(self): return self
    def begin(self): return nullcontext()
    def execution_options(self, **kwargs): return self
    def execute(self, sql, parameters=None):
        sql = str(sql)
        if "SELECT n.nspname AS schema_name, c.relname AS name" in sql: return Rows(self.relations)
        if "FROM pg_proc p JOIN" in sql and "SELECT 'function'" not in sql: return Rows(self.functions)
        if "FROM pg_trigger t JOIN" in sql and "UNION ALL" not in sql: return Rows(self.triggers)
        if "SELECT version_num" in sql: return Rows(self.versions)
        if "pg_export_snapshot()" in sql:
            self.exported = True
            pytest.fail("unsafe catalog reached exported snapshot")
        if "SHOW server_version_num" in sql: return type("Scalar", (), {"scalar_one": lambda s: "170011"})()
        return Rows()


def test_catalog_rejects_missing_guards_before_export():
    from backend.tests.ops.backup.test_project_guards import catalog
    _, _, relations = catalog(3)
    relations += [{"schema_name":"public", "name":"alembic_version", "kind":"r"}]
    relations = [dict(r, oid=i+1, persistence="p", tablespace=0) for i,r in enumerate(relations)]
    connection = CatalogConnection(relations=relations)
    with pytest.raises(BackupError) as caught:
        with module().snapshot_inventory(connection, timeout=5): pytest.fail("accepted missing protection")
    assert caught.value.exit_code == 2 and not connection.exported


def test_unguarded_structure_hash_is_legacy_exact():
    class Reader:
        def execute(self, *args, **kwargs): return Rows()
    legacy = b'{"columns":[],"constraints":[],"indexes":[]}'
    assert module()._structure(None, {"oid":1}, Reader()) == hashlib.sha256(legacy).hexdigest()
    assert module()._structure(None, {"oid":9}, Reader(), guards=()) == hashlib.sha256(legacy).hexdigest()


def test_guarded_structure_binds_guard_contract():
    from backend.tests.ops.backup.test_project_guards import catalog, validate
    functions, triggers, relations = catalog()
    guards = validate(functions, triggers, relations)[("ops", "enterprise_rea_plan")]
    class Reader:
        def execute(self, *args, **kwargs): return Rows()
    expected = {"columns":[], "constraints":[], "indexes":[], "projectImmutableGuards":[asdict(v) for v in guards]}
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    assert module()._structure(None, {"oid":1}, Reader(), guards=guards) == digest
    assert digest != module()._structure(None, {"oid":1}, Reader())


def test_project_guard_queries_use_remaining_budget(monkeypatch):
    from app.ops.backup.project_guards import inspect_project_guards
    inventory = module()
    clock, observed = [0.0], []
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    class Connection(CatalogConnection):
        def execute(self, sql, parameters=None):
            if "statement_timeout" in str(sql): observed.append(int(parameters["value"]))
            elif "FROM pg_" in str(sql): clock[0] += 0.6
            return super().execute(sql, parameters)
    relations = [{"schema_name":"public", "name":"alembic_version", "kind":"r"}]
    with pytest.raises(BackupError) as caught: inspect_project_guards(Connection(), inventory.Budget(1), relations)
    assert caught.value.reason_code == "backup_budget_exhausted"
    assert observed == [1000,400]
