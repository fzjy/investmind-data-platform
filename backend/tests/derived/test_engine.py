from datetime import date
from decimal import Decimal


from app.db.models import Security
from app.stocks.derived.engine import MetricEngine
from app.stocks.derived.registry import calculate_formula


def seed_security(factory):
    with factory.begin() as session:
        session.add(Security(ts_code="000001.SZ", symbol="000001", name="平安银行"))


def test_metric_engine_is_idempotent_and_records_inputs(pg_factory) -> None:
    factory = pg_factory
    seed_security(factory)
    engine = MetricEngine(factory)
    result = engine.calculate_inputs("000001.SZ", date(2025, 12, 31), {"cfo_ttm": 20, "parent_profit_ttm": 10}, ("realMoneyIndex",))
    again = engine.calculate_inputs("000001.SZ", date(2025, 12, 31), {"cfo_ttm": 20, "parent_profit_ttm": 10}, ("realMoneyIndex",))
    assert result[0].value == Decimal("2.0000")
    assert again[0].value == result[0].value


def test_growth_formula_registry_maps_named_inputs() -> None:
    assert calculate_formula(
        "profitGrowth",
        {"profit": Decimal("10"), "previous_profit": Decimal("8")},
    ).value == Decimal("25.0000")
