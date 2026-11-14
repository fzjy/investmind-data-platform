from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, DerivedBase, MartBase
from app.db.models import RiskResult, Security
from app.stocks.derived.engine import MetricEngine
from app.stocks.derived.registry import calculate_formula


def _factory() -> sessionmaker:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    bind = engine.execution_options(schema_translate_map={"raw": None, "core": None, "ops": None, "derived": None, "mart": None})
    Base.metadata.create_all(bind)
    DerivedBase.metadata.create_all(bind)
    MartBase.metadata.create_all(bind)
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Security(ts_code="000001.SZ", symbol="000001", name="平安银行"))
    return factory


def test_metric_engine_is_idempotent_and_records_inputs() -> None:
    factory = _factory()
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
