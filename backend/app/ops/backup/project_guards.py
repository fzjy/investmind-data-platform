"""仅承认仓库已知迁移的完整保护图；不是通用函数白名单。"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from typing import Protocol

from sqlalchemy import Connection, Result

from app.ops.backup.contracts import BackupError


USER_NAMESPACE = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'"
UNGUARDED_REVISIONS = (
    "20260817_0001", "20260817_0002", "20260817_0003", "20260820_0004",
    "20260824_0005", "20260825_0006", "20260902_0007", "20260905_0008",
    "20260920_0009", "20260929_0010", "20261014_0011", "20261015_0012",
)
GUARD_COUNTS = {
    **dict.fromkeys(UNGUARDED_REVISIONS, 0),
    "20261018_0013": 1, "20261020_0014": 2, "20261021_0015": 3, "20261022_0016": 4,
    "20261022_0017": 5, "20261110_0018": 5, "20261111_0019": 5, "20261111_0020": 5, "20261115_0021": 5, "20261116_0022": 5,
}


@dataclass(frozen=True)
class GuardContract:
    suffix: str
    marker: str
    tables: tuple[tuple[str, str], ...]

    @property
    def function_name(self) -> str:
        return f"reject_enterprise_{self.suffix}_mutation"

    @property
    def trigger_name(self) -> str:
        return f"immutable_enterprise_{self.suffix}"

    @property
    def body(self) -> str:
        return f"BEGIN RAISE EXCEPTION '{self.marker}'; END"


CONTRACTS = (
    GuardContract("receivable", "ENTERPRISE_RECEIVABLE_IMMUTABLE", (("raw", "enterprise_receivable_response"), ("core", "enterprise_receivable_supplement"))),
    GuardContract("order", "ENTERPRISE_ORDER_IMMUTABLE", (("raw", "enterprise_order_response"), ("core", "enterprise_order_supplement"))),
    GuardContract("rea", "ENTERPRISE_REA_IMMUTABLE", (("ops", "enterprise_rea_plan"), ("raw", "enterprise_rea_response"), ("core", "enterprise_rea_supplement"))),
    GuardContract("target", "TARGET_EVIDENCE_IMMUTABLE", (("raw", "enterprise_target_observation"), ("core", "enterprise_target_candidate"))),
    GuardContract("target_execution", "TARGET_EXECUTION_IMMUTABLE", (("ops", "enterprise_target_execution_plan"),)),
)
FUNCTION_ATTRIBUTES: dict[str, object] = dict(language="plpgsql", return_schema="pg_catalog", return_name="trigger",
    prokind="f", pronargs=0, pronargdefaults=0, proargtypes="", proallargtypes=None, proargmodes=None,
    proargnames=None, proargdefaults=None, provariadic=0, proretset=False, prosecdef=False, proleakproof=False,
    proisstrict=False, provolatile="v", proparallel="u", procost=100.0, prorows=0.0, proconfig=None,
    probin=None, prosqlbody=None, protrftypes=None, prosupport=0)
TRIGGER_ATTRIBUTES: dict[str, object] = dict(function_nargs=0, tgtype=27, tgenabled="O", tgisinternal=False,
    tgattr="", tgqual=None, tgnargs=0, tgargs="", tgconstraint=0, tgconstrrelid=0, tgconstrindid=0,
    tgparentid=0, tgdeferrable=False, tginitdeferred=False, tgoldtable=None, tgnewtable=None)


@dataclass(frozen=True)
class ProjectGuardDefinition:
    schema_name: str
    table_name: str
    trigger_name: str
    function_schema: str
    function_name: str
    function_sha256: str
    function_attributes: tuple[tuple[str, object], ...]
    trigger_attributes: tuple[tuple[str, object], ...]
    contract_version: str = "project-immutable-guards-v1"


def _reject() -> None:
    raise BackupError("unsupported_database_objects", 2, "inventory")


def _attributes_match(row: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    for key, value in expected.items():
        if key not in row or row[key] != value:
            return False
        if isinstance(value, bool) and type(row[key]) is not bool:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(row[key], bool):
            return False
    return True


def validate_project_guards(
    functions: Sequence[Mapping[str, object]], triggers: Sequence[Mapping[str, object]],
    versions: tuple[str, ...] | None, relations: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], tuple[ProjectGuardDefinition, ...]]:
    names = {(r["schema_name"], r["name"]) for r in relations if r["kind"] == "r"}
    all_tables = {t for contract in CONTRACTS for t in contract.tables}
    if versions is None:
        if functions or triggers or names & all_tables:
            _reject()
        return {}
    if len(versions) != 1 or versions[0] not in GUARD_COUNTS:
        _reject()
    active = CONTRACTS[:GUARD_COUNTS[versions[0]]]
    expected_tables = {t for contract in active for t in contract.tables}
    if names & all_tables != expected_tables or len(functions) != len(active) or len(triggers) != len(expected_tables):
        _reject()
    remaining_functions = list(functions)
    remaining_triggers = list(triggers)
    result: dict[tuple[str, str], tuple[ProjectGuardDefinition, ...]] = {}
    for contract in active:
        matching = [f for f in remaining_functions if f.get("schema_name") == "ops" and f.get("name") == contract.function_name]
        if len(matching) != 1:
            _reject()
        function = matching[0]
        body = function.get("prosrc")
        if not _attributes_match(function, FUNCTION_ATTRIBUTES) or not isinstance(body, str) or " ".join(body.split()) != contract.body:
            _reject()
        remaining_functions.remove(function)
        for schema, table in contract.tables:
            matching_triggers = [t for t in remaining_triggers if t.get("schema_name") == schema and t.get("table_name") == table and t.get("name") == contract.trigger_name]
            if len(matching_triggers) != 1:
                _reject()
            trigger = matching_triggers[0]
            if trigger.get("function_schema") != "ops" or trigger.get("function_name") != contract.function_name or not _attributes_match(trigger, TRIGGER_ATTRIBUTES):
                _reject()
            remaining_triggers.remove(trigger)
            result[(schema, table)] = (ProjectGuardDefinition(schema, table, contract.trigger_name, "ops", contract.function_name,
                hashlib.sha256(contract.body.encode("ascii")).hexdigest(), tuple(sorted(FUNCTION_ATTRIBUTES.items())), tuple(sorted(TRIGGER_ATTRIBUTES.items()))),)
    if remaining_functions or remaining_triggers:
        _reject()
    return dict(sorted(result.items()))


class CatalogReader(Protocol):
    def execute(self, connection: Connection, sql: str, parameters: Mapping[str, object] | None = None, *, stream: bool = False) -> Result: ...


def inspect_project_guards(connection: Connection, budget: CatalogReader, relations: Sequence[Mapping[str, object]]) -> dict[tuple[str, str], tuple[ProjectGuardDefinition, ...]]:
    functions = list(budget.execute(connection, f"""
        SELECT n.nspname AS schema_name, p.proname AS name, l.lanname AS language,
            rn.nspname AS return_schema, rt.typname AS return_name,
            p.prokind, p.pronargs, p.pronargdefaults, p.proargtypes::text,
            p.proallargtypes, p.proargmodes, p.proargnames, p.proargdefaults::text,
            p.provariadic::oid, p.proretset, p.prosecdef, p.proleakproof, p.proisstrict,
            p.provolatile, p.proparallel, p.procost, p.prorows, p.proconfig,
            p.probin, p.prosqlbody::text, p.protrftypes::text, p.prosupport::oid, p.prosrc
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang JOIN pg_type rt ON rt.oid=p.prorettype
        JOIN pg_namespace rn ON rn.oid=rt.typnamespace
        WHERE {USER_NAMESPACE} ORDER BY n.nspname COLLATE "C", p.proname COLLATE "C", p.oid
    """).mappings())
    triggers = list(budget.execute(connection, f"""
        SELECT n.nspname AS schema_name, c.relname AS table_name, t.tgname AS name,
            fn.nspname AS function_schema, f.proname AS function_name, f.pronargs AS function_nargs,
            t.tgtype, t.tgenabled, t.tgisinternal, t.tgattr::text, t.tgqual::text,
            t.tgnargs, encode(t.tgargs,'hex') AS tgargs, t.tgconstraint, t.tgconstrrelid,
            t.tgconstrindid, t.tgparentid, t.tgdeferrable, t.tginitdeferred, t.tgoldtable, t.tgnewtable
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
        WHERE {USER_NAMESPACE} AND NOT t.tgisinternal
        ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C", t.tgname COLLATE "C"
    """).mappings())
    has_version = any(r["schema_name"] == "public" and r["name"] == "alembic_version" and r["kind"] == "r" for r in relations)
    versions = tuple(budget.execute(connection, "SELECT version_num FROM public.alembic_version ORDER BY version_num").scalars()) if has_version else None
    return validate_project_guards(functions, triggers, versions, relations)
