from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, DerivedBase, ExternalBase, MartBase
from app.db.models import MetricInput, MetricValue, RiskResult, Security
from app.stocks.derived.inputs import CoreSnapshot, InputSource, SecurityFormulaInputs
from app.stocks.derived.pipeline import DerivedPipeline
from app.stocks.derived.registry import FORMULAS


def _factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        execution_options={
            "schema_translate_map": {
                "raw": None,
                "core": None,
                "ops": None,
                "derived": None,
                "mart": None,
            }
        },
    )
    Base.metadata.create_all(engine)
    DerivedBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            Security(
                ts_code="600519.SH",
                symbol="600519",
                name="贵州茅台",
                status="L",
            )
        )
    return factory


def _snapshot() -> CoreSnapshot:
    values: dict[str, object] = {
        "cfo_ttm": Decimal("20"),
        "parent_profit_ttm": Decimal("10"),
        "capex_ttm": Decimal("5"),
        "nopat": Decimal("8"),
        "invested_capital": Decimal("100"),
        "pe": Decimal("10"),
        "pe_history_3y": (Decimal("5"), Decimal("10"), Decimal("15")),
        "pb": Decimal("2"),
        "pb_history_3y": (Decimal("1"), Decimal("2"), Decimal("3")),
        "dividend_fiscal_years": (2023, 2024, 2025),
        "as_of_year": 2026,
        "cash_dividend": Decimal("2"),
        "parent_profit": Decimal("10"),
        "dividends": Decimal("2"),
        "buybacks": Decimal("1"),
        "equity_financing_inflow": Decimal("10"),
        "latest_dps": Decimal("1"),
        "dividend_history": (Decimal("1"), Decimal("1"), Decimal("1")),
        "close": Decimal("100"),
        "institution_shares": Decimal("10"),
        "float_shares": Decimal("100"),
        "total_debt": Decimal("20"),
        "cash": Decimal("10"),
        "profit": Decimal("10"),
        "previous_profit": Decimal("8"),
        "revenue": Decimal("100"),
        "previous_revenue": Decimal("90"),
        "cfo": Decimal("20"),
        "previous_cfo": Decimal("18"),
    }
    sources = {
        name: InputSource(
            "core.daily_basic" if name in {"pe", "pb"} else "core.income_statement",
            f"600519.SH:2026-03-31:{name}",
            value,
        )
        for name, value in values.items()
        if name in {input_name for definition in FORMULAS.values() for input_name in definition.inputs}
    }
    return CoreSnapshot(
        as_of=date(2026, 3, 31),
        securities=(),
        daily_prices=(),
        daily_basics=(),
        adjustment_factors=(),
        security_inputs={
            "600519.SH": SecurityFormulaInputs(
                values=values,
                sources=sources,
                risk_values={
                    "inventory_ratio": Decimal("0.2"),
                    "goodwill_ratio": Decimal("0.1"),
                    "net_debt_ratio": Decimal("0.5"),
                    "cashflow_dividend_ratio": Decimal("2"),
                },
            )
        },
        missing_data=(),
        financial_points=(),
        corporate_actions=(),
        classifications=(),
        macro_observations=(),
        content_documents=(),
    )


def test_pipeline_persists_real_lineage_and_leaves_risks_to_workbench_publication() -> None:
    factory = _factory()
    pipeline = DerivedPipeline(factory)

    first = pipeline.run(_snapshot(), ("600519.SH",))
    second = pipeline.run(_snapshot(), ("600519.SH",))

    with factory() as session:
        risks = session.scalars(select(RiskResult)).all()
        inputs = session.scalars(select(MetricInput)).all()

    assert first.metric_count == second.metric_count == 14
    assert first.risk_count == second.risk_count == 0
    assert not risks
    assert {item.source_table for item in inputs} >= {
        "core.daily_basic",
        "core.income_statement",
    }
    assert all(
        item.source_record_key != "600519.SH:2026-03-31" for item in inputs
    )


def test_lineage_count_ignores_historical_formula_versions() -> None:
    factory = _factory()
    pipeline = DerivedPipeline(factory)
    first = pipeline.run(_snapshot(), ("600519.SH",))
    with factory.begin() as session:
        old = MetricValue(
            ts_code="600519.SH", metric_id="realMoneyIndex", as_of=date(2026, 3, 31),
            value=Decimal("2"), unit="ratio", formula_version="0.9.0", quality="ok", missing_fields=[],
        )
        session.add(old)
        session.flush()
        session.add(MetricInput(
            metric_value_id=old.id, input_name="legacy", source_table="legacy",
            source_record_key="legacy:1", content_hash="x",
        ))

    second = pipeline.run(_snapshot(), ("600519.SH",))

    assert first.declared_lineage_count == second.persisted_lineage_count
