from decimal import Decimal

from app.stocks.derived.formulas import (
    capital_return_ratio,
    dividend_years,
    expected_dividend_yield,
    historical_percentile,
    real_money_index,
    roic,
)


def test_real_money_returns_null_for_loss() -> None:
    result = real_money_index(Decimal("10"), Decimal("-1"))
    assert result.value is None
    assert result.quality == "not_applicable"


def test_percentile_uses_only_positive_three_year_values() -> None:
    result = historical_percentile(Decimal("10"), [Decimal("-2"), Decimal("5"), Decimal("10"), Decimal("20")])
    assert result.value == Decimal("66.6667")


def test_roic_is_not_applicable_to_banks() -> None:
    assert roic(10, 100, industry="bank").quality == "not_applicable"


def test_dividend_and_capital_return_require_valid_denominator() -> None:
    assert dividend_years([2022, 2023, 2024]).value == Decimal("3")
    assert capital_return_ratio(10, 5, 0).value is None


def test_expected_yield_requires_three_years() -> None:
    result = expected_dividend_yield(1, [1, 1], 10)
    assert result.value is None
