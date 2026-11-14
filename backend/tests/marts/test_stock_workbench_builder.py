from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.db.models import AdjustmentFactor, DailyPrice, Security
from app.stocks.derived.inputs import FinancialPoint
from app.stocks.marts.stocks import StockWorkbenchMartBuilder, prepare_workbench_inputs
from app.marts.dto import validate_product_payload
from backend.tests.marts.test_screener_builder import _core_snapshot


class _CountingFactors:
    def __init__(self, rows: tuple[AdjustmentFactor, ...]) -> None:
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.rows)


class _CountingSecurities:
    def __init__(self, rows: tuple[Security, ...]) -> None:
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.rows)


def test_workbench_contains_daily_only_real_sections() -> None:
    candidate = StockWorkbenchMartBuilder().build(
        _core_snapshot(),
        "600519.SH",
        "v1",
    )

    assert candidate.payload["quote"]["price"] == 1500
    assert candidate.payload["dailyChart"]["candles"]
    assert candidate.payload["dataAvailability"]["intraday"]["available"] is False


def test_missing_quote_change_stays_null_in_quote_and_sidebar() -> None:
    snapshot = _core_snapshot()
    for row in snapshot.daily_prices:
        row.pct_chg = None
    payload = StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "v1").payload
    assert payload["quote"]["changePercent"] is None
    assert payload["sidebar"]["stocks"][0]["changePercent"] is None
    assert validate_product_payload("stock-workbench:600519.SH", payload) == ()


def test_workbench_matches_nested_frontend_contract() -> None:
    payload = StockWorkbenchMartBuilder().build(
        _core_snapshot(),
        "600519.SH",
        "v1",
    ).payload

    assert set(payload["sidebar"]) == {"referenceDate", "events", "stocks"}
    assert set(payload["quote"]) >= {
        "symbol", "name", "market", "industry", "subIndustry", "mentions",
        "latestMention", "concepts", "price", "changePercent", "pe", "pb",
        "previousClose", "intraday",
    }
    assert set(payload["metrics"]) == {"cards", "years", "fields", "presets"}
    assert set(payload["risks"]) == {"score", "level", "groups", "assessment"}
    assert set(payload["financial"]["dashboard"]) == {
        "products", "productTrend", "bands", "peg", "trends", "dcf", "returnHistory", "operatingHistory",
    }
    assert set(payload["financial"]["shareholders"]) == {
        "holders", "dividendSeries", "returnSeries", "concentrationSeries",
        "northboundSeries", "executiveTrades",
    }
    assert validate_product_payload("stock-workbench:600519.SH", payload) == ()


def test_workbench_contract_rejects_nested_null_number() -> None:
    payload = StockWorkbenchMartBuilder().build(
        _core_snapshot(),
        "600519.SH",
        "v1",
    ).payload
    payload["quote"]["mentions"] = None

    assert "quote.mentions" in validate_product_payload(
        "stock-workbench:600519.SH",
        payload,
    )


def test_workbench_uses_latest_factor_adjusted_ohlc() -> None:
    base = _core_snapshot()
    snapshot = replace(
        base,
        daily_prices=(
            DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 23), open=Decimal("48"), high=Decimal("52"), low=Decimal("47"), close=Decimal("50"), vol=Decimal("100")),
            DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 24), open=Decimal("98"), high=Decimal("102"), low=Decimal("97"), close=Decimal("100"), vol=Decimal("100")),
        ),
        adjustment_factors=(
            AdjustmentFactor(ts_code="600519.SH", trade_date=date(2026, 8, 23), adj_factor=Decimal("1")),
            AdjustmentFactor(ts_code="600519.SH", trade_date=date(2026, 8, 24), adj_factor=Decimal("2")),
        ),
    )

    candles = StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "v1").payload["dailyChart"]["candles"]

    assert candles[0]["close"] == 25
    assert candles[1]["close"] == 100


def test_workbench_indexes_adjustment_factors_once_for_all_candles() -> None:
    base = _core_snapshot()
    factors = _CountingFactors(
        (
            AdjustmentFactor(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 23),
                adj_factor=Decimal("1"),
            ),
            AdjustmentFactor(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                adj_factor=Decimal("2"),
            ),
        )
    )
    snapshot = replace(
        base,
        daily_prices=(
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 23),
                open=Decimal("48"),
                high=Decimal("52"),
                low=Decimal("47"),
                close=Decimal("50"),
            ),
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                open=Decimal("98"),
                high=Decimal("102"),
                low=Decimal("97"),
                close=Decimal("100"),
            ),
        ),
        adjustment_factors=factors,
    )

    StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "v1")

    assert factors.iterations == 1


def test_workbench_indexes_securities_once_for_repeated_builds() -> None:
    base = _core_snapshot()
    securities = _CountingSecurities(base.securities)
    snapshot = replace(base, securities=securities)
    builder = StockWorkbenchMartBuilder()
    prepared = prepare_workbench_inputs(snapshot)

    builder.build(snapshot, "600519.SH", "v1", prepared_inputs=prepared)
    builder.build(snapshot, "600519.SH", "v1", prepared_inputs=prepared)

    assert securities.iterations == 1


def test_workbench_uses_latest_visible_financial_revision() -> None:
    snapshot = replace(
        _core_snapshot(),
        financial_points=(
            FinancialPoint(
                end_date=date(2025, 12, 31),
                visible_at=date(2026, 3, 1),
                report_type="1",
                values={"total_revenue": 100, "update_flag": "0"},
                ts_code="600519.SH",
                source_table="core.income_statement",
                source_record_key="old",
            ),
            FinancialPoint(
                end_date=date(2025, 12, 31),
                visible_at=date(2026, 4, 1),
                report_type="1",
                values={"total_revenue": 200, "update_flag": "1"},
                ts_code="600519.SH",
                source_table="core.income_statement",
                source_record_key="new",
            ),
        ),
    )

    payload = StockWorkbenchMartBuilder().build(
        snapshot,
        "600519.SH",
        "v1",
    ).payload
    income = next(
        table
        for table in payload["financial"]["statements"]["tables"]
        if table["kind"] == "income"
    )
    revenue = next(row for row in income["rows"] if row["label"] == "total_revenue")

    assert revenue["values"] == [200]


def test_workbench_amount_fields_use_yi_yuan_but_raw_statements_preserve_source_units() -> None:
    snapshot = replace(_core_snapshot(), financial_points=(
        FinancialPoint(end_date=date(2025, 12, 31), visible_at=date(2026, 3, 1), report_type="1",
            values={"total_revenue": Decimal("123456789"), "n_income_attr_p": Decimal("100000000"), "basic_eps": Decimal("1.25")},
            ts_code="600519.SH", source_table="core.income_statement", source_record_key="unit-proof"),
    ))
    payload = StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "v1").payload
    fields = {f["id"]: f["values"] for f in payload["metrics"]["fields"]}
    assert fields["revenue"] == [1.23456789]
    assert fields["parentNetProfit"] == [1]
    table = payload["financial"]["statements"]["tables"][0]
    assert table["amountUnit"] == "yuan"
    rows = {r["label"]: r["values"] for r in table["rows"]}
    assert rows["total_revenue"] == [123456789]
    assert rows["basic_eps"] == [1.25]


def test_ratio_fields_keep_partial_years_and_exclude_parent_only_reports() -> None:
    def point(year, kind, values):
        return FinancialPoint(date(year, 12, 31), date(year + 1, 3, 1), kind, values,
                              "600519.SH", "core.income_statement", str(year) + kind)

    snapshot = replace(_core_snapshot(), financial_points=(
        point(2024, "1", {"revenue": 0}),
        point(2024, "6", {"n_income_attr_p": 200}),
        point(2025, "1", {"revenue": 1e8, "n_income_attr_p": 3e8}),
    ))
    metrics = StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "v1").payload["metrics"]
    fields = {row["id"]: row["values"] for row in metrics["fields"]}
    assert fields["parentNetProfit"] == [None, 3]
    assert fields["revenue"] == [0, 1]
    assert "interestBearingDebt" not in fields and "deductNetProfit" not in fields
