"""固定迁移对象图：同名不是保护证明。"""
from copy import deepcopy
from dataclasses import asdict
import importlib

import pytest

from app.ops.backup.contracts import BackupError


GROUPS = (
    ("receivable", "ENTERPRISE_RECEIVABLE_IMMUTABLE", (("raw", "enterprise_receivable_response"), ("core", "enterprise_receivable_supplement"))),
    ("order", "ENTERPRISE_ORDER_IMMUTABLE", (("raw", "enterprise_order_response"), ("core", "enterprise_order_supplement"))),
    ("rea", "ENTERPRISE_REA_IMMUTABLE", (("ops", "enterprise_rea_plan"), ("raw", "enterprise_rea_response"), ("core", "enterprise_rea_supplement"))),
    ("target", "TARGET_EVIDENCE_IMMUTABLE", (("raw", "enterprise_target_observation"), ("core", "enterprise_target_candidate"))),
    ("target_execution", "TARGET_EXECUTION_IMMUTABLE", (("ops", "enterprise_target_execution_plan"),)),
)
REVISIONS = ("20261018_0013", "20261020_0014", "20261021_0015", "20261022_0016", "20261022_0017")


def module():
    from app.ops import backup
    assert importlib.util.find_spec("app.ops.backup.project_guards") is not None, "strict project guard validator missing"
    return importlib.import_module("app.ops.backup.project_guards")


def catalog(groups=4):
    functions, triggers, relations = [], [], []
    for suffix, marker, tables in GROUPS[:groups]:
        functions.append(dict(schema_name="ops", name=f"reject_enterprise_{suffix}_mutation", language="plpgsql",
            return_schema="pg_catalog", return_name="trigger", prokind="f", pronargs=0, pronargdefaults=0,
            proargtypes="", proallargtypes=None, proargmodes=None, proargnames=None, proargdefaults=None,
            provariadic=0, proretset=False, prosecdef=False, proleakproof=False, proisstrict=False,
            provolatile="v", proparallel="u", procost=100.0, prorows=0.0, proconfig=None,
            probin=None, prosqlbody=None, protrftypes=None, prosupport=0,
            prosrc=f" BEGIN\n RAISE EXCEPTION '{marker}'; END "))
        for schema, table in tables:
            relations.append(dict(schema_name=schema, name=table, kind="r"))
            triggers.append(dict(schema_name=schema, table_name=table, name=f"immutable_enterprise_{suffix}",
                function_schema="ops", function_name=f"reject_enterprise_{suffix}_mutation", function_nargs=0,
                tgtype=27, tgenabled="O", tgisinternal=False, tgattr="", tgqual=None,
                tgnargs=0, tgargs="", tgconstraint=0, tgconstrrelid=0, tgconstrindid=0,
                tgparentid=0, tgdeferrable=False, tginitdeferred=False, tgoldtable=None, tgnewtable=None))
    return functions, triggers, relations


def validate(functions, triggers, relations, versions=("20261022_0016",)):
    return module().validate_project_guards(functions, triggers, versions, relations)


@pytest.mark.parametrize("groups,bindings", [(1,2), (2,4), (3,7), (4,9), (5,10)])
def test_exact_revision_guard_graph(groups, bindings):
    functions, triggers, relations = catalog(groups)
    result = validate(functions, triggers, relations, (REVISIONS[groups-1],))
    assert len(result) == bindings
    assert sum(len(v) for v in result.values()) == bindings
    assert all(asdict(v[0])["contract_version"] == "project-immutable-guards-v1" for v in result.values())


def test_clean_generic_database_and_known_legacy_revision():
    assert validate([], [], [], None) == {}
    assert validate([], [], [], ("20261015_0012",)) == {}


@pytest.mark.parametrize("version", ["20261110_0018", "20261111_0019", "20261111_0020"])
def test_later_revisions_require_all_five_guard_groups(version):
    assert len(validate(*catalog(5), (version,))) == 10
    with pytest.raises(BackupError):
        validate(*catalog(4), (version,))


@pytest.mark.parametrize("change", ["no-version", "empty-version", "unknown-version", "multi-head", "all-removed",
    "old-version", "missing-function", "missing-trigger", "missing-table", "extra-function", "extra-trigger", "wrong-schema", "missing-version-field"])
def test_guard_graph_fails_closed(change):
    functions, triggers, relations = catalog()
    versions = ("20261022_0016",)
    if change == "no-version": versions = None
    if change == "empty-version": versions = ()
    if change == "unknown-version": versions = ("20261023_0017",)
    if change == "multi-head": versions += ("20261021_0015",)
    if change == "all-removed": functions, triggers = [], []
    if change == "old-version": versions = ("20261015_0012",)
    if change == "missing-function": functions.pop()
    if change == "missing-trigger": triggers.pop()
    if change == "missing-table": relations.pop()
    if change == "extra-function": functions.append(deepcopy(functions[0]))
    if change == "extra-trigger": triggers.append(deepcopy(triggers[0]))
    if change == "wrong-schema": functions[0]["schema_name"] = "public"
    if change == "missing-version-field": versions = (None,)
    with pytest.raises(BackupError) as caught:
        validate(functions, triggers, relations, versions)
    assert caught.value.reason_code == "unsupported_database_objects" and caught.value.exit_code == 2


FUNCTION_MUTATIONS = {
    "schema_name":"public", "name":"other", "language":"sql", "return_schema":"public", "return_name":"text",
    "prokind":"p", "pronargs":1, "pronargdefaults":1, "proargtypes":"23", "proallargtypes":[23],
    "proargmodes":["i"], "proargnames":["a"], "proargdefaults":"1", "provariadic":23, "proretset":True,
    "prosecdef":True, "proleakproof":True, "proisstrict":True, "provolatile":"i", "proparallel":"s",
    "procost":1.0, "prorows":1.0, "proconfig":["search_path=public"], "probin":"unsafe", "prosqlbody":"unsafe",
    "protrftypes":"23", "prosupport":1, "prosrc":"BEGIN RETURN NEW; END",
}
TRIGGER_MUTATIONS = {
    "schema_name":"public", "table_name":"other", "name":"other", "function_schema":"public", "function_name":"other",
    "function_nargs":1, "tgtype":19, "tgenabled":"D", "tgisinternal":True, "tgattr":"1", "tgqual":"(true)",
    "tgnargs":1, "tgargs":"00", "tgconstraint":1, "tgconstrrelid":1, "tgconstrindid":1, "tgparentid":1,
    "tgdeferrable":True, "tginitdeferred":True, "tgoldtable":"old", "tgnewtable":"new",
}


@pytest.mark.parametrize("kind,key,value", [("f",k,v) for k,v in FUNCTION_MUTATIONS.items()] + [("t",k,v) for k,v in TRIGGER_MUTATIONS.items()] + [("t","tgenabled",v) for v in ("A","R")])
def test_guard_attributes_are_exact(kind, key, value):
    functions, triggers, relations = catalog()
    (functions if kind == "f" else triggers)[0][key] = value
    with pytest.raises(BackupError): validate(functions, triggers, relations)


@pytest.mark.parametrize("kind,key", [("f",k) for k in FUNCTION_MUTATIONS] + [("t",k) for k in TRIGGER_MUTATIONS])
def test_missing_catalog_attribute_is_not_legal_null(kind, key):
    functions, triggers, relations = catalog()
    del (functions if kind == "f" else triggers)[0][key]
    with pytest.raises(BackupError): validate(functions, triggers, relations)


@pytest.mark.parametrize("body", ["BEGIN RETURN NEW; END", "BEGIN /* comment */ RAISE EXCEPTION 'ENTERPRISE_RECEIVABLE_IMMUTABLE'; END", "BEGIN RAISE EXCEPTION 'ENTERPRISE_RECEIVABLE_IMMUTABLE'; RETURN NEW; END"])
def test_function_body_cannot_add_code_or_comments(body):
    functions, triggers, relations = catalog()
    functions[0]["prosrc"] = body
    with pytest.raises(BackupError): validate(functions, triggers, relations)


def test_guard_fingerprint_ignores_only_physical_identity():
    functions, triggers, relations = catalog()
    before = validate(functions, triggers, relations)
    functions[0].update(oid=99999, owner="not-collected")
    triggers[0].update(oid=88888)
    assert validate(functions, triggers, relations) == before
    payload = str([asdict(v) for values in before.values() for v in values])
    assert "not-collected" not in payload and "RAISE EXCEPTION" not in payload
