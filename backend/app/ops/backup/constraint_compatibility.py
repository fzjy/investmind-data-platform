"""PG数组转换分配的七项精确合同，不是SQL正则/通用表达式等价器。"""

_STATES = ("reserved", "received", "failed")
_SUPPLEMENT = ("matched", "returned-null", "source-witness-missing", "response-witness-missing",
    "no-match", "ambiguous", "missing-field", "invalid-value", "truncated", "identity-mismatch")
_REA = ("matched", "response-witness-missing", "no-match", "ambiguous", "truncated", "identity-mismatch")
_CHECKS = {
    ("ops", "enterprise_receivable_attempt", "receivable_attempt_state"): ("state", _STATES),
    ("ops", "enterprise_order_attempt", "order_attempt_state"): ("state", _STATES),
    ("ops", "enterprise_rea_attempt", "rea_attempt_state"): ("state", _STATES),
    ("core", "enterprise_receivable_supplement", "receivable_supplement_status"): ("status", _SUPPLEMENT),
    ("core", "enterprise_order_supplement", "order_supplement_status"): ("status", _SUPPLEMENT),
    ("core", "enterprise_rea_supplement", "rea_supplement_status"): ("status", _REA),
    ("core", "market_daily_quote", "ck_market_quote_asset_type"): ("asset_type", ("indices", "etfs")),
}
COMPATIBLE_TABLES = frozenset((schema, table) for schema, table, _ in _CHECKS)

# 同一迁移在单metadata naming_convention下产生的已核对完整名称。
# 不剥前缀、不按后缀匹配、不猜测SQLAlchemy的截断hash；原conname仍参与结构hash。
_CONVENTION_NAMES = {
    ("ops", "enterprise_receivable_attempt", "ck_enterprise_receivable_attempt_receivable_attempt_state"): "receivable_attempt_state",
    ("ops", "enterprise_order_attempt", "ck_enterprise_order_attempt_order_attempt_state"): "order_attempt_state",
    ("ops", "enterprise_rea_attempt", "ck_enterprise_rea_attempt_rea_attempt_state"): "rea_attempt_state",
    ("core", "enterprise_receivable_supplement", "ck_enterprise_receivable_supplement_receivable_suppleme_1d57"): "receivable_supplement_status",
    ("core", "enterprise_order_supplement", "ck_enterprise_order_supplement_order_supplement_status"): "order_supplement_status",
    ("core", "enterprise_rea_supplement", "ck_enterprise_rea_supplement_rea_supplement_status"): "rea_supplement_status",
    ("core", "market_daily_quote", "ck_market_daily_quote_ck_market_quote_asset_type"): "ck_market_quote_asset_type",
}


def canonical_restored_check(schema: str, table: str, name: str, expression: str) -> str:
    contract_name = _CONVENTION_NAMES.get((schema, table, name), name)
    contract = _CHECKS.get((schema, table, contract_name))
    if contract is None:
        return expression
    column, values = contract
    distributed = ", ".join(f"'{v}'::character varying::text" for v in values)
    if expression != f"CHECK ({column}::text = ANY (ARRAY[{distributed}]))":
        return expression
    original = ", ".join(f"'{v}'::character varying" for v in values)
    return f"CHECK ({column}::text = ANY (ARRAY[{original}]::text[]))"
