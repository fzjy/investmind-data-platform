from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, DerivedBase, ExternalBase, MartBase
from app.db.models import (
    Classification,
    ClassificationMember,
    AdjustmentFactor,
    BalanceSheet,
    BusinessSegment,
    DailyBasic,
    DailyPrice,
    Dividend,
    FinancialIndicator,
    IncomeStatement,
    Security,
    ShareUnlock,
    TopHolder,
)
from app.stocks.derived.inputs import (
    CoreSnapshotRepository,
    FinancialPoint,
    InputSource,
    _ttm,
    canonical_source_hash,
    ttm_value,
    visible_financial_rows,
)


def test_repository_connects_visible_profitability_inputs_and_lineage() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="财务样本", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 3, 31), close=Decimal("10")))
        session.add_all([
            FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 12, 31),
                ann_date=date(2026, 3, 20), report_type="1", update_flag="0",
                data={"roe": 11, "grossprofit_margin": 30, "netprofit_margin": 20}),
            FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 12, 31),
                ann_date=date(2026, 3, 20), report_type="1", update_flag="1",
                data={"roe": 12, "grossprofit_margin": 31, "netprofit_margin": 21}),
            FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 12, 31),
                ann_date=date(2026, 3, 21), report_type="2", data={"roe": 90}),
            FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 12, 31),
                ann_date=date(2026, 4, 20), report_type="1", data={"roe": 99}),
            FinancialIndicator(ts_code="600519.SH", end_date=date(2026, 3, 31),
                ann_date=None, report_type="1", data={"roe": 100}),
        ])
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 3, 31), ("600519.SH",))
    inputs = snapshot.security_inputs["600519.SH"]
    for field, expected in (("roe", 12), ("grossprofit_margin", 31), ("netprofit_margin", 21)):
        assert inputs.values.get(field) == Decimal(expected)
        source = inputs.sources[field]
        assert source.source_table == "core.financial_indicator"
        assert source.source_record_key == "600519.SH:2025-12-31:1:2026-03-20:1"
        assert source.value == Decimal(expected)
        assert len(source.content_hash) == 64
    from app.stocks.marts.stocks import ScreenerMartBuilder
    stock = ScreenerMartBuilder().build(snapshot, "future-candidate").payload["stocks"][0]
    assert (stock["roe"], stock["grossMargin"], stock["netMargin"]) == (12, 31, 21)


@pytest.mark.parametrize("roe", [None, 0, -3])
def test_profitability_inputs_do_not_fill_missing_current_values_from_prior_period(roe) -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="财务样本", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 3, 31), close=Decimal("10")))
        session.add_all([
            FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 9, 30),
                ann_date=date(2025, 10, 20), report_type="1",
                data={"roe": 50, "grossprofit_margin": 50, "netprofit_margin": 50}),
            FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 12, 31),
                ann_date=date(2026, 3, 20), report_type="1",
                data={"roe": roe, "grossprofit_margin": roe, "netprofit_margin": roe}),
        ])
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 3, 31), ("600519.SH",))
    inputs = snapshot.security_inputs["600519.SH"]
    for field in ("roe", "grossprofit_margin", "netprofit_margin"):
        assert field in inputs.values
        assert inputs.values[field] == roe
        assert inputs.sources[field].value == roe
        assert inputs.sources[field].source_record_key == "600519.SH:2025-12-31:1:2026-03-20:"
    from app.stocks.marts.stocks import ScreenerMartBuilder
    row = ScreenerMartBuilder().build(snapshot, "preview").payload["stocks"][0]
    assert (row["roe"], row["grossMargin"], row["netMargin"]) == (roe, roe, roe)


@pytest.mark.parametrize("announced,actual", [
    (None, None), (date(2026, 4, 20), None), (date(2026, 3, 20), date(2026, 4, 20)),
])
def test_profitability_without_visible_disclosure_keeps_null_with_missing_lineage(announced, actual):
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="财务样本", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 3, 31), close=Decimal("10")))
        session.add(FinancialIndicator(ts_code="600519.SH", end_date=date(2025, 12, 31),
            ann_date=announced, f_ann_date=actual, report_type="1",
            data={"roe": 99, "grossprofit_margin": 99, "netprofit_margin": 99}))
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 3, 31), ("600519.SH",))
    inputs = snapshot.security_inputs["600519.SH"]
    for field in ("roe", "grossprofit_margin", "netprofit_margin"):
        assert inputs.values[field] is None
        assert inputs.sources[field].value is None
        assert inputs.sources[field].source_table == "core.financial_indicator"
        assert inputs.sources[field].source_record_key == f"600519.SH:missing:{field}"
    from app.stocks.marts.stocks import ScreenerMartBuilder
    row = ScreenerMartBuilder().build(snapshot, "preview").payload["stocks"][0]
    assert (row["roe"], row["grossMargin"], row["netMargin"]) == (None, None, None)


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
    return sessionmaker(engine, expire_on_commit=False)


def test_visible_financial_rows_excludes_future_revision() -> None:
    rows = (
        FinancialPoint(
            date(2025, 12, 31),
            date(2026, 3, 20),
            "1",
            {"revenue": Decimal("100")},
        ),
        FinancialPoint(
            date(2025, 12, 31),
            date(2026, 4, 20),
            "1",
            {"revenue": Decimal("120")},
        ),
    )

    selected = visible_financial_rows(rows, date(2026, 3, 31))

    assert selected[0].values["revenue"] == Decimal("100")


def test_visible_financial_rows_prefers_update_flag_for_same_announcement() -> None:
    rows = (
        FinancialPoint(
            date(2025, 12, 31),
            date(2026, 3, 20),
            "1",
            {"revenue": Decimal("120"), "update_flag": "1"},
            source_record_key="updated",
        ),
        FinancialPoint(
            date(2025, 12, 31),
            date(2026, 3, 20),
            "1",
            {"revenue": Decimal("100"), "update_flag": "0"},
            source_record_key="initial",
        ),
    )

    selected = visible_financial_rows(rows, date(2026, 3, 31))

    assert selected[0].values["revenue"] == Decimal("120")


def test_ttm_value_uses_current_ytd_plus_prior_annual_minus_prior_ytd() -> None:
    assert ttm_value(Decimal("30"), Decimal("100"), Decimal("20")) == Decimal("110")


def test_ttm_matches_prior_period_date_and_report_type() -> None:
    points = (
        FinancialPoint(date(2025, 9, 30), date(2025, 10, 20), "1", {"revenue": 30}),
        FinancialPoint(date(2024, 12, 31), date(2025, 3, 20), "1", {"revenue": 100}),
        FinancialPoint(date(2024, 9, 30), date(2024, 10, 20), "1", {"revenue": 20}),
        FinancialPoint(date(2024, 9, 30), date(2024, 10, 21), "2", {"revenue": 90}),
    )

    value, sources = _ttm(points, ("revenue",))

    assert value == Decimal("110")
    assert {point.report_type for point in sources} == {"1"}


def test_ttm_prefers_consolidated_report_type_independent_of_input_order() -> None:
    report_one = (
        FinancialPoint(date(2025, 9, 30), date(2025, 10, 20), "1", {"revenue": 30}),
        FinancialPoint(date(2024, 12, 31), date(2025, 3, 20), "1", {"revenue": 100}),
        FinancialPoint(date(2024, 9, 30), date(2024, 10, 20), "1", {"revenue": 20}),
    )
    report_two = (
        FinancialPoint(date(2025, 9, 30), date(2025, 10, 20), "2", {"revenue": 200}),
        FinancialPoint(date(2024, 12, 31), date(2025, 3, 20), "2", {"revenue": 50}),
        FinancialPoint(date(2024, 9, 30), date(2024, 10, 20), "2", {"revenue": 30}),
    )

    forward, _ = _ttm(report_one + report_two, ("revenue",))
    reverse, _ = _ttm(tuple(reversed(report_one + report_two)), ("revenue",))

    assert forward == reverse == Decimal("110")


def test_ttm_does_not_treat_year_end_single_quarter_as_annual_value() -> None:
    points = (
        FinancialPoint(
            date(2025, 12, 31),
            date(2026, 3, 20),
            "2",
            {"revenue": 30},
        ),
    )

    value, sources = _ttm(points, ("revenue",))

    assert value is None
    assert sources == points


def test_missing_input_has_deterministic_null_lineage() -> None:
    source = InputSource("core.balance_sheet", "600519.SH:20251231", None)

    assert source.content_hash == canonical_source_hash(
        None,
        "core.balance_sheet",
        "600519.SH:20251231",
    )


def test_repository_builds_visible_ttm_inputs_and_excludes_future_rows() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(
            Security(
                ts_code="600519.SH",
                symbol="600519",
                name="贵州茅台",
                exchange="SSE",
                status="L",
            )
        )
        session.add_all(
            [
                DailyPrice(
                    ts_code="600519.SH",
                    trade_date=date(2026, 3, 31),
                    close=Decimal("1500"),
                ),
                DailyPrice(
                    ts_code="600519.SH",
                    trade_date=date(2026, 4, 20),
                    close=Decimal("1600"),
                ),
                DailyBasic(
                    ts_code="600519.SH",
                    trade_date=date(2026, 3, 31),
                    pe=Decimal("20"),
                    data={},
                ),
            ]
        )
        session.add_all(
            [
                IncomeStatement(
                    ts_code="600519.SH",
                    end_date=date(2025, 9, 30),
                    ann_date=date(2025, 10, 25),
                    report_type="1",
                    data={"n_income_attr_p": 30},
                ),
                IncomeStatement(
                    ts_code="600519.SH",
                    end_date=date(2024, 12, 31),
                    ann_date=date(2025, 4, 1),
                    report_type="1",
                    data={"n_income_attr_p": 100},
                ),
                IncomeStatement(
                    ts_code="600519.SH",
                    end_date=date(2024, 9, 30),
                    ann_date=date(2024, 10, 25),
                    report_type="1",
                    data={"n_income_attr_p": 20},
                ),
                IncomeStatement(
                    ts_code="600519.SH",
                    end_date=date(2025, 9, 30),
                    ann_date=date(2026, 4, 20),
                    report_type="1",
                    data={"n_income_attr_p": 80},
                ),
            ]
        )

    snapshot = CoreSnapshotRepository(factory).load(
        date(2026, 3, 31),
        ("600519.SH",),
    )
    inputs = snapshot.security_inputs["600519.SH"]

    assert inputs.values["parent_profit_ttm"] == Decimal("110")
    assert inputs.values["pe"] == Decimal("20")
    assert inputs.sources["parent_profit_ttm"].source_table == "core.income_statement"
    assert all(row.trade_date <= date(2026, 3, 31) for row in snapshot.daily_prices)
    assert date(2026, 4, 20) not in {row.visible_at for row in snapshot.financial_points}


def test_repository_prefers_newer_visible_annual_report_and_matches_prior_annual() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台", exchange="SSE", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 3, 31), close=Decimal("1500")))
        session.add_all([
            IncomeStatement(ts_code="600519.SH", end_date=date(2025, 9, 30), ann_date=date(2025, 10, 20), report_type="1", data={"n_income_attr_p": 30, "total_revenue": 300}),
            IncomeStatement(ts_code="600519.SH", end_date=date(2025, 12, 31), ann_date=date(2026, 3, 20), report_type="1", data={"n_income_attr_p": 120, "total_revenue": 1200}),
            IncomeStatement(ts_code="600519.SH", end_date=date(2024, 12, 31), ann_date=date(2025, 3, 20), report_type="1", data={"n_income_attr_p": 100, "total_revenue": 1000}),
            IncomeStatement(ts_code="600519.SH", end_date=date(2024, 9, 30), ann_date=date(2024, 10, 20), report_type="1", data={"n_income_attr_p": 20, "total_revenue": 200}),
        ])

    values = CoreSnapshotRepository(factory).load(date(2026, 3, 31), ("600519.SH",)).security_inputs["600519.SH"].values

    assert values["parent_profit_ttm"] == Decimal("120")
    assert values["revenue_ttm"] == Decimal("1200")
    assert values["profit"] == 120
    assert values["previous_profit"] == 100
    assert values["revenue"] == 1200
    assert values["previous_revenue"] == 1000


def test_repository_handles_leap_day_for_three_year_history() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台", exchange="SSE", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2024, 2, 29), close=Decimal("1500")))
        session.add(DailyBasic(ts_code="600519.SH", trade_date=date(2024, 2, 29), pe=Decimal("20"), pb=Decimal("5"), data={}))

    values = CoreSnapshotRepository(factory).load(date(2024, 2, 29), ("600519.SH",)).security_inputs["600519.SH"].values

    assert values["pe_history_3y"] == (Decimal("20"),)


def test_repository_limits_daily_market_rows_to_three_year_window() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(
            Security(
                ts_code="600519.SH",
                symbol="600519",
                name="贵州茅台",
                exchange="SSE",
                status="L",
            )
        )
        for model, value_field in (
            (DailyPrice, "close"),
            (DailyBasic, "pe"),
            (AdjustmentFactor, "adj_factor"),
        ):
            session.add_all(
                [
                    model(
                        ts_code="600519.SH",
                        trade_date=date(2023, 8, 23),
                        **{value_field: Decimal("1")},
                    ),
                    model(
                        ts_code="600519.SH",
                        trade_date=date(2023, 8, 24),
                        **{value_field: Decimal("2")},
                    ),
                    model(
                        ts_code="600519.SH",
                        trade_date=date(2026, 8, 24),
                        **{value_field: Decimal("3")},
                    ),
                ]
            )

    snapshot = CoreSnapshotRepository(factory).load(
        date(2026, 8, 24),
        ("600519.SH",),
    )

    assert {row.trade_date for row in snapshot.daily_prices} == {
        date(2023, 8, 24),
        date(2026, 8, 24),
    }
    assert {row.trade_date for row in snapshot.daily_basics} == {
        date(2023, 8, 24),
        date(2026, 8, 24),
    }
    assert {row.trade_date for row in snapshot.adjustment_factors} == {
        date(2023, 8, 24),
        date(2026, 8, 24),
    }


def test_repository_applies_point_in_time_security_holder_and_classification_filters() -> None:
    factory = _factory()
    with factory.begin() as session:
        security = Security(
            ts_code="600519.SH", symbol="600519", name="贵州茅台", exchange="SSE",
            status="D", list_date=date(2001, 8, 27), delist_date=date(2027, 1, 1),
        )
        future = Security(
            ts_code="688001.SH", symbol="688001", name="未来上市", exchange="SSE",
            status="L", list_date=date(2027, 1, 1),
        )
        session.add_all([security, future])
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 24), close=Decimal("1500")))
        session.add(TopHolder(
            ts_code="600519.SH", end_date=date(2025, 12, 31), ann_date=date(2026, 9, 1),
            holder_type="机构", holder_name="未来公告机构", hold_amount=Decimal("100"), data={},
        ))
        classification = Classification(provider="x", classification_id="food", name="食品饮料", level=1, data={})
        session.add(classification)
        session.flush()
        session.add(ClassificationMember(
            classification_id=classification.id, ts_code="600519.SH",
            effective_from=date(2020, 1, 1), effective_to=date(2025, 12, 31),
        ))

    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))

    assert [row.ts_code for row in snapshot.securities] == ["600519.SH"]
    assert snapshot.security_inputs["600519.SH"].values["institution_shares"] is None
    assert snapshot.classification_members == ()


def test_repository_never_promotes_legacy_cash_without_query_and_tax_fields() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台", exchange="SSE", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 24), close=Decimal("1500")))
        session.add(DailyBasic(
            ts_code="600519.SH", trade_date=date(2026, 6, 1),
            data={"total_share": 100},
        ))
        session.add_all([
            Dividend(
                ts_code="600519.SH", end_date=date(2025, 12, 31), ann_date=date(2026, 3, 1),
                div_proc="预案", cash_div=Decimal("2"), ex_date=date(2026, 6, 1), data={},
            ),
            Dividend(
                ts_code="600519.SH", end_date=date(2025, 12, 31), ann_date=date(2026, 5, 1),
                div_proc="实施", cash_div=Decimal("1"), ex_date=date(2026, 6, 1), data={},
            ),
            Dividend(
                ts_code="600519.SH", end_date=date(2025, 12, 31), ann_date=date(2026, 5, 2),
                div_proc="实施完成", cash_div=Decimal("1"), ex_date=date(2026, 6, 1), data={},
            ),
        ])

    values = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",)).security_inputs["600519.SH"].values

    assert values["cash_dividend"] is None
    assert values["dividends"] is None
    assert values["latest_dps"] is None


def test_repository_replaces_older_dividend_event_revision() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(
            Security(
                ts_code="600519.SH",
                symbol="600519",
                name="贵州茅台",
                exchange="SSE",
                status="L",
            )
        )
        session.add(
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                close=Decimal("1500"),
            )
        )
        session.add(
            DailyBasic(
                ts_code="600519.SH",
                trade_date=date(2026, 6, 1),
                data={"total_share": 100},
            )
        )
        session.add_all(
            [
                Dividend(
                    ts_code="600519.SH",
                    end_date=date(2025, 12, 31),
                    ann_date=date(2026, 5, 1),
                    div_proc="实施",
                    cash_div=Decimal("1.0"),
                    record_date=date(2026, 5, 31),
                    ex_date=date(2026, 6, 1),
                    pay_date=date(2026, 6, 2),
                    data={},
                ),
                Dividend(
                    ts_code="600519.SH",
                    end_date=date(2025, 12, 31),
                    ann_date=date(2026, 5, 2),
                    div_proc="实施完成",
                    cash_div=Decimal("1.2"),
                    record_date=date(2026, 5, 31),
                    ex_date=date(2026, 6, 1),
                    pay_date=date(2026, 6, 2),
                    data={},
                ),
            ]
        )

    values = CoreSnapshotRepository(factory).load(
        date(2026, 8, 24),
        ("600519.SH",),
    ).security_inputs["600519.SH"].values

    assert values["cash_dividend"] is None
    assert values["dividends"] is None
    assert values["latest_dps"] is None


def test_repository_uses_latest_visible_holder_period_and_action_dates() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台", exchange="SSE", status="L"))
        session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 24), close=Decimal("1500")))
        session.add_all([
            TopHolder(ts_code="600519.SH", end_date=date(2024, 12, 31), ann_date=date(2025, 3, 1), holder_type="机构", holder_name="旧机构", hold_amount=Decimal("100"), data={}),
            TopHolder(ts_code="600519.SH", end_date=date(2025, 12, 31), ann_date=date(2026, 3, 1), holder_type="机构", holder_name="新机构", hold_amount=Decimal("150"), data={}),
            ShareUnlock(ts_code="600519.SH", ann_date=date(2026, 1, 1), float_date=date(2026, 9, 1), data={}),
            BusinessSegment(ts_code="600519.SH", end_date=date(2025, 12, 31), segment_type="P", segment_name="白酒", revenue=Decimal("1000"), data={}),
        ])

    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))

    assert snapshot.security_inputs["600519.SH"].values["institution_shares"] == Decimal("150")
    assert not any(isinstance(row, ShareUnlock) for row in snapshot.corporate_actions)
    assert any(row.source_table == "core.business_segment" for row in snapshot.financial_points)


def test_repository_normalizes_json_float_before_decimal_risk_ratio() -> None:
    factory = _factory()
    with factory.begin() as session:
        session.add(
            Security(
                ts_code="600519.SH",
                symbol="600519",
                name="贵州茅台",
                exchange="SSE",
                status="L",
            )
        )
        session.add(
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                close=Decimal("1500"),
            )
        )
        session.add(
            BalanceSheet(
                ts_code="600519.SH",
                end_date=date(2025, 12, 31),
                ann_date=date(2026, 3, 20),
                report_type="1",
                data={"total_hldr_eqy_exc_min_int": 500.0},
            )
        )
        session.add(
            FinancialIndicator(
                ts_code="600519.SH",
                end_date=date(2025, 12, 31),
                ann_date=date(2026, 3, 20),
                report_type="1",
                data={"netdebt": 100.0},
            )
        )

    snapshot = CoreSnapshotRepository(factory).load(
        date(2026, 8, 24),
        ("600519.SH",),
    )

    assert snapshot.security_inputs["600519.SH"].risk_values[
        "net_debt_ratio"
    ] == Decimal("0.2")
