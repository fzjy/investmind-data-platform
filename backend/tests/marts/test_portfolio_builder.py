from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.db.models import AdjustmentFactor, DailyPrice, Security
from app.portfolio.marts.portfolio import PortfolioMartBuilder


def test_portfolio_exposes_unadjusted_prices_separately_for_share_costs() -> None:
    snapshot = _core_snapshot()
    payload = PortfolioMartBuilder().build(snapshot, "v-cost").payload
    expected = [{"date": row.trade_date.isoformat(), "price": float(row.close)}
                for row in snapshot.daily_prices if row.ts_code == "600519.SH"]
    assert payload["unadjustedPrices"]["600519.SH"] == expected
    assert payload["unadjustedPrices"]["CNY"][0]["price"] == 1


def test_optional_listing_date_uses_iso_or_omits_unknown() -> None:
    snapshot = _core_snapshot()
    snapshot.securities[0].list_date = None
    asset = PortfolioMartBuilder().build(snapshot, "missing-listing").payload["assets"][0]
    assert "listDate" not in asset
    snapshot.securities[0].list_date = date(2001, 8, 27)
    asset = PortfolioMartBuilder().build(snapshot, "known-listing").payload["assets"][0]
    assert asset["listDate"] == "2001-08-27"
from backend.tests.marts.test_screener_builder import _core_snapshot


class _CountingFactors:
    def __init__(self, rows: tuple[AdjustmentFactor, ...]) -> None:
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.rows)


class _CountingPrices:
    def __init__(self, rows: tuple[DailyPrice, ...]) -> None:
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.rows)


def test_portfolio_uses_real_stock_prices_and_definitionally_fixed_cash() -> None:
    candidate = PortfolioMartBuilder().build(_core_snapshot(), "v1")

    assert {asset["id"] for asset in candidate.payload["assets"]} == {
        "600519.SH",
        "CNY",
    }
    assert candidate.payload["prices"]["CNY"] == [
        {"date": "2026-08-24", "price": 1}
    ]


def test_portfolio_uses_same_latest_factor_adjustment_as_workbench() -> None:
    base = _core_snapshot()
    snapshot = replace(
        base,
        daily_prices=(
            DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 23), close=Decimal("50")),
            DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 24), close=Decimal("100")),
        ),
        adjustment_factors=(
            AdjustmentFactor(ts_code="600519.SH", trade_date=date(2026, 8, 23), adj_factor=Decimal("1")),
            AdjustmentFactor(ts_code="600519.SH", trade_date=date(2026, 8, 24), adj_factor=Decimal("2")),
        ),
    )

    prices = PortfolioMartBuilder().build(snapshot, "v1").payload["prices"]["600519.SH"]

    assert prices == [
        {"date": "2026-08-23", "price": 25},
        {"date": "2026-08-24", "price": 100},
    ]


def test_factor_audit_is_per_stock_and_requires_current_base():
    import json

    base = _core_snapshot()
    stock, clean = "600519.SH", "000001.SZ"
    dates = [date(2026, 8, d) for d in range(18, 25)]
    prices = tuple(DailyPrice(ts_code=symbol, trade_date=d, close=Decimal("10"))
                   for symbol in (stock, clean) for d in dates)
    factors = tuple(AdjustmentFactor(ts_code=clean, trade_date=d, adj_factor=Decimal("1")) for d in dates)
    values = [Decimal("1"), None, Decimal("0"), Decimal("NaN"), Decimal("Infinity")]
    factors += tuple(AdjustmentFactor(ts_code=stock, trade_date=d, adj_factor=v) for d, v in zip(dates, values))
    snapshot = replace(base, securities=(*base.securities, Security(ts_code=clean, symbol="000001", name="平安银行")),
                       daily_prices=prices, adjustment_factors=factors)
    payload = PortfolioMartBuilder().build(snapshot, "v-audit").payload
    audit = payload["factorCoverage"]
    assert audit[clean]["baseValid"] and not audit[clean]["issues"]
    assert not audit[stock]["baseValid"]
    assert audit[stock]["issues"] == [
        {"date": d.isoformat(), "reason": "invalid" if d in dates[1:5] else "missing"} for d in dates[1:]
    ]
    json.dumps(payload, allow_nan=False)
    # Even a valid previous observation cannot certify a missing terminal base.
    previous = AdjustmentFactor(ts_code=stock, trade_date=dates[-2], adj_factor=Decimal("1"))
    rebuilt = PortfolioMartBuilder().build(replace(snapshot, adjustment_factors=(*factors, previous)), "v-old-base")
    assert not rebuilt.payload["factorCoverage"][stock]["baseValid"]


def test_portfolio_indexes_adjustment_factors_once_for_all_prices() -> None:
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
                close=Decimal("50"),
            ),
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                close=Decimal("100"),
            ),
        ),
        adjustment_factors=factors,
    )

    PortfolioMartBuilder().build(snapshot, "v1")

    assert factors.iterations == 1


def test_portfolio_indexes_daily_prices_once_for_all_securities() -> None:
    base = _core_snapshot()
    prices = _CountingPrices(
        (
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                close=Decimal("100"),
            ),
            DailyPrice(
                ts_code="000001.SZ",
                trade_date=date(2026, 8, 24),
                close=Decimal("10"),
            ),
        )
    )
    snapshot = replace(
        base,
        securities=(
            base.securities[0],
            Security(
                ts_code="000001.SZ",
                symbol="000001",
                name="平安银行",
                exchange="SZSE",
                status="L",
            ),
        ),
        daily_prices=prices,
    )

    PortfolioMartBuilder().build(snapshot, "v1")

    assert prices.iterations == 1
