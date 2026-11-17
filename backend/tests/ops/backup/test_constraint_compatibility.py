"""只接受PG17已观察到的固定枚举CHECK数组转换等价形式。"""
import importlib
import hashlib
import json

import pytest

from app.ops.backup import inventory
from app.ops.backup.contracts import BackupError, DatabaseInventory, TableInventory
from backend.tests.ops.backup.test_inventory import Rows


ORIGINAL = "CHECK (state::text = ANY (ARRAY['reserved'::character varying, 'received'::character varying, 'failed'::character varying]::text[]))"
RESTORED = "CHECK (state::text = ANY (ARRAY['reserved'::character varying::text, 'received'::character varying::text, 'failed'::character varying::text]))"

# 独立来源：真实源/隔离恢复的只读目录，不从生产白名单生成期望。
CONVENTION_CASES = (
    ("ops","enterprise_receivable_attempt","receivable_attempt_state","ck_enterprise_receivable_attempt_receivable_attempt_state","state",("reserved","received","failed")),
    ("ops","enterprise_order_attempt","order_attempt_state","ck_enterprise_order_attempt_order_attempt_state","state",("reserved","received","failed")),
    ("ops","enterprise_rea_attempt","rea_attempt_state","ck_enterprise_rea_attempt_rea_attempt_state","state",("reserved","received","failed")),
    ("core","enterprise_receivable_supplement","receivable_supplement_status","ck_enterprise_receivable_supplement_receivable_suppleme_1d57","status",("matched","returned-null","source-witness-missing","response-witness-missing","no-match","ambiguous","missing-field","invalid-value","truncated","identity-mismatch")),
    ("core","enterprise_order_supplement","order_supplement_status","ck_enterprise_order_supplement_order_supplement_status","status",("matched","returned-null","source-witness-missing","response-witness-missing","no-match","ambiguous","missing-field","invalid-value","truncated","identity-mismatch")),
    ("core","enterprise_rea_supplement","rea_supplement_status","ck_enterprise_rea_supplement_rea_supplement_status","status",("matched","response-witness-missing","no-match","ambiguous","truncated","identity-mismatch")),
    ("core","market_daily_quote","ck_market_quote_asset_type","ck_market_daily_quote_ck_market_quote_asset_type","asset_type",("indices","etfs")),
)


@pytest.mark.parametrize("schema,table,raw_name,convention_name,column,values",CONVENTION_CASES)
def test_exact_known_migration_convention_names_only(schema,table,raw_name,convention_name,column,values):
    normalize = compat().canonical_restored_check
    distributed = ", ".join(f"'{v}'::character varying::text" for v in values)
    elements = ", ".join(f"'{v}'::character varying" for v in values)
    restored = f"CHECK ({column}::text = ANY (ARRAY[{distributed}]))"
    original = f"CHECK ({column}::text = ANY (ARRAY[{elements}]::text[]))"
    assert normalize(schema,table,convention_name,restored) == original
    assert normalize(schema,table,raw_name,restored) == original
    for bad_name in ("unknown_"+convention_name,convention_name+"_other",convention_name.replace("ck_","ck_wrong_",1)):
        assert normalize(schema,table,bad_name,restored) == restored
    for bad_schema,bad_table in (("unknown",table),(schema,"unknown")):
        assert normalize(bad_schema,bad_table,convention_name,restored) == restored
    assert normalize(schema,table,convention_name,restored+" OR true") == restored+" OR true"
    changed = restored.replace("'"+values[-1]+"'","'unexpected'")
    assert normalize(schema,table,convention_name,changed) == changed


def test_scope_kind_requires_exact_identity_values_and_order():
    normalize = compat().canonical_restored_check
    identity = ("ops", "data_acquisition_state", "scope_kind")
    restored = "CHECK (scope_kind::text = ANY (ARRAY['routine'::character varying::text, 'historical'::character varying::text]))"
    original = "CHECK (scope_kind::text = ANY (ARRAY['routine'::character varying, 'historical'::character varying]::text[]))"
    assert normalize(*identity, restored) == original
    for idx in range(len(identity)):
        changed = list(identity)
        changed[idx] += "_other"
        assert normalize(*changed, restored) == restored
    for invalid in (restored.replace("historical", "other"), restored.replace("routine", "tmp").replace("historical", "routine").replace("tmp", "historical")):
        assert normalize(*identity, invalid) == invalid


def compat():
    assert hasattr(inventory, "assert_restored_inventory"), "restore semantic comparison missing"
    return importlib.import_module("app.ops.backup.constraint_compatibility")


def test_exact_known_check_cast_shape_only():
    normalize = compat().canonical_restored_check
    assert normalize("ops","enterprise_receivable_attempt","receivable_attempt_state",RESTORED) == ORIGINAL
    for schema, table, name, expression in [
        ("public","enterprise_receivable_attempt","receivable_attempt_state",RESTORED),
        ("ops","other","receivable_attempt_state",RESTORED),
        ("ops","enterprise_receivable_attempt","other",RESTORED),
        ("ops","enterprise_receivable_attempt","receivable_attempt_state",RESTORED.replace("'failed'","'bad'")),
        ("ops","enterprise_receivable_attempt","receivable_attempt_state",RESTORED+" OR true"),
    ]:
        assert normalize(schema,table,name,expression) == expression


def test_original_source_schema_hash_is_unchanged_but_restore_alternative_matches():
    constraint = {"conname":"receivable_attempt_state", "definition":RESTORED}
    class Reader:
        def execute(self, connection, sql, parameters=None): return Rows([constraint]) if "FROM pg_constraint" in sql else Rows()
    relation = {"oid":1,"schema_name":"ops","name":"enterprise_receivable_attempt"}
    legacy_json = json.dumps({"columns":[],"constraints":[constraint],"indexes":[]}, sort_keys=True, ensure_ascii=True, separators=(",",":"))
    assert inventory._structure(None,relation,Reader()) == hashlib.sha256(legacy_json.encode()).hexdigest()
    original_json = legacy_json.replace(RESTORED,ORIGINAL)
    assert inventory._structure(None,relation,Reader(),restore_compatibility=True) == hashlib.sha256(original_json.encode()).hexdigest()


@pytest.mark.parametrize("change", ["rows", "extra-table", "schema", "checks", "none"])
def test_restore_semantic_match_still_binds_entire_inventory(monkeypatch, change):
    compat()
    expected_table = TableInventory(schema_name="ops",table_name="enterprise_receivable_attempt",row_count=1,rows_sha256="a"*64,schema_sha256="b"*64)
    expected = DatabaseInventory(tables=(expected_table,),schemas=("ops",))
    actual_table = expected_table.model_copy(update={"schema_sha256":"c"*64})
    actual = expected.model_copy(update={"tables":(actual_table,)})
    if change == "rows": actual = actual.model_copy(update={"tables":(actual_table.model_copy(update={"rows_sha256":"f"*64}),)})
    if change == "extra-table": actual = actual.model_copy(update={"tables":(actual_table,actual_table.model_copy(update={"table_name":"unexpected"}))})
    if change == "schema": actual = actual.model_copy(update={"schemas":("ops","extra")})
    monkeypatch.setattr(inventory,"_catalog",lambda *a:inventory.CatalogView(({"schema_name":"ops","name":"enterprise_receivable_attempt","kind":"r","oid":1},),{}))
    monkeypatch.setattr(inventory,"_structure",lambda *a,**kw:"d"*64 if change=="checks" else "b"*64)
    if change == "none": inventory.assert_restored_inventory(None,actual,expected,timeout=5)
    else:
        with pytest.raises(BackupError) as caught: inventory.assert_restored_inventory(None,actual,expected,timeout=5)
        assert caught.value.reason_code == "restored_inventory_mismatch"
