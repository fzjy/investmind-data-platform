from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping


ZERO = Decimal("0")
FOUR_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class MetricResult:
    value: Decimal | None
    quality: str = "ok"
    missing: tuple[str, ...] = ()
    unit: str | None = None

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return self.missing


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _missing(*names: str) -> MetricResult:
    return MetricResult(None, "missing_input", tuple(names))


def _round(value: Decimal | None) -> Decimal | None:
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP) if value is not None else None


def real_money_index(cfo: object, parent_profit: object) -> MetricResult:
    cfo_value, profit = _decimal(cfo), _decimal(parent_profit)
    missing = tuple(name for name, value in (("cfo_ttm", cfo_value), ("parent_profit_ttm", profit)) if value is None)
    if missing:
        return _missing(*missing)
    if profit <= ZERO:
        return MetricResult(None, "not_applicable")
    return MetricResult(_round(cfo_value / profit), unit="ratio")


def free_cash_flow(cfo: object, capex: object) -> MetricResult:
    cfo_value, capex_value = _decimal(cfo), _decimal(capex)
    missing = tuple(name for name, value in (("cfo_ttm", cfo_value), ("capex_ttm", capex_value)) if value is None)
    if missing:
        return _missing(*missing)
    return MetricResult(_round(cfo_value - capex_value), unit="currency")


def roic(
    nopat: object,
    invested_capital: object,
    prior_invested_capital: object | None = None,
    industry: str | None = None,
) -> MetricResult:
    if industry and industry.lower() in {"bank", "banks", "insurance", "证券", "银行", "保险"}:
        return MetricResult(None, "not_applicable")
    nopat_value, capital = _decimal(nopat), _decimal(invested_capital)
    missing = tuple(name for name, value in (("nopat", nopat_value), ("invested_capital", capital)) if value is None)
    if missing:
        return _missing(*missing)
    prior = _decimal(prior_invested_capital)
    denominator = (capital + prior) / Decimal("2") if prior is not None else capital
    if denominator <= ZERO:
        return MetricResult(None, "not_applicable")
    return MetricResult(_round(nopat_value / denominator), unit="ratio")


def historical_percentile(current: object, history: Iterable[object], window_years: int = 3) -> MetricResult:
    current_value = _decimal(current)
    if current_value is None:
        return _missing("current")
    values = [value for value in (_decimal(item) for item in history) if value is not None and value > ZERO]
    if not values:
        return _missing("positive_history")
    # The rank is the share of positive observations not above the current value.
    rank = sum(value <= current_value for value in values)
    return MetricResult(_round(Decimal(rank) / Decimal(len(values)) * Decimal("100")), unit="percent")


def dividend_years(dividend_fiscal_years: Iterable[int], as_of_year: int | None = None) -> MetricResult:
    if isinstance(dividend_fiscal_years, Mapping):
        from app.stocks.derived.metric_dividends import paid_cash_year_available
        records = dividend_fiscal_years
        anchor = as_of_year - 1 if as_of_year is not None else max((int(y) for y in records), default=0)
        count = 0
        for year in range(anchor, 0, -1):
            entry = records.get(str(year), {})
            if not paid_cash_year_available(entry):
                return _missing(f'{year}:paid_cash_records')
            if not any(e.get('status') == 'paid' and (_decimal(e.get('cashPerShare')) or ZERO) > ZERO
                       for e in entry.get('events', [])):
                return MetricResult(Decimal(count), unit='years')
            count += 1
            if entry.get('listedAt') and year == int(entry['listedAt'][:4]):
                return MetricResult(Decimal(count), unit='years')
        return _missing('paid_cash_records')
    if dividend_fiscal_years is None:
        return _missing('dividend_fiscal_years')
    years = sorted({int(year) for year in dividend_fiscal_years})
    if not years:
        return _missing("dividend_fiscal_years")
    if as_of_year is not None:
        years = [year for year in years if year <= as_of_year]
    if not years:
        return _missing("completed_fiscal_year")
    count = 1
    for index in range(len(years) - 1, 0, -1):
        if years[index] - years[index - 1] != 1:
            break
        count += 1
    return MetricResult(Decimal(count), unit="years")


def payout_ratio(cash_dividend: object, parent_profit: object) -> MetricResult:
    dividend, profit = _decimal(cash_dividend), _decimal(parent_profit)
    missing = tuple(name for name, value in (("cash_dividend", dividend), ("parent_profit", profit)) if value is None)
    if missing:
        return _missing(*missing)
    if profit <= ZERO:
        return MetricResult(None, "not_applicable")
    return MetricResult(_round(dividend / profit * Decimal("100")), unit="percent")


def capital_return_ratio(dividends: object, buybacks: object, equity_financing_inflow: object) -> MetricResult:
    dividend, repurchase, financing = map(_decimal, (dividends, buybacks, equity_financing_inflow))
    missing = tuple(name for name, value in (("dividends", dividend), ("buybacks", repurchase), ("equity_financing_inflow", financing)) if value is None)
    if missing:
        return _missing(*missing)
    if financing <= ZERO:
        return MetricResult(None, "not_applicable")
    return MetricResult(_round((dividend + repurchase) / financing * Decimal("100")), unit="percent")


def expected_dividend_yield(
    latest_dps: object,
    dividend_history: Iterable[object],
    close: object,
    growth_cap: Decimal = Decimal("0.20"),
) -> MetricResult:
    dps, close_value = _decimal(latest_dps), _decimal(close)
    history = [value for value in (_decimal(item) for item in dividend_history) if value is not None and value >= ZERO]
    missing = tuple(name for name, value in (("latest_dps", dps), ("close", close_value)) if value is None)
    if missing:
        return _missing(*missing)
    if len(history) < 3:
        return MetricResult(None, "missing_input", ("three_year_dividend_history",))
    if close_value <= ZERO:
        return MetricResult(None, "not_applicable")
    oldest, newest = history[-3], history[-1]
    if oldest > ZERO:
        growth = (newest / oldest) ** (Decimal("1") / Decimal("2")) - Decimal("1")
    else:
        growth = ZERO
    growth = min(max(growth, -growth_cap), growth_cap)
    return MetricResult(_round(dps * (Decimal("1") + growth) / close_value * Decimal("100")), unit="percent")


def institution_holding(institution_shares: object, institution_total_shares: object) -> MetricResult:
    held, total = _decimal(institution_shares), _decimal(institution_total_shares)
    missing = tuple(name for name, value in (("institution_shares", held), ("institution_total_shares", total)) if value is None)
    if missing:
        return _missing(*missing)
    if not held.is_finite() or not total.is_finite() or held < ZERO or held > total:
        return MetricResult(None, "invalid_input")
    if total <= ZERO:
        return MetricResult(None, "not_applicable")
    return MetricResult(_round(held / total * Decimal("100")), "partial_coverage", unit="percent")


def net_debt(total_debt: object, cash: object) -> MetricResult:
    debt, cash_value = _decimal(total_debt), _decimal(cash)
    missing = tuple(name for name, value in (("total_debt", debt), ("cash", cash_value)) if value is None)
    if missing:
        return _missing(*missing)
    return MetricResult(_round(debt - cash_value), unit="currency")


def yoy_growth(current: object, previous: object) -> MetricResult:
    current_value, previous_value = _decimal(current), _decimal(previous)
    missing = tuple(name for name, value in (("current", current_value), ("previous", previous_value)) if value is None)
    if missing:
        return _missing(*missing)
    if previous_value == ZERO:
        return MetricResult(None, "not_applicable")
    return MetricResult(_round((current_value - previous_value) / abs(previous_value) * Decimal("100")), unit="percent")


def is_declining(current: object, previous: object) -> MetricResult:
    growth = yoy_growth(current, previous)
    if growth.value is None:
        return growth
    return MetricResult(Decimal("1") if growth.value < ZERO else Decimal("0"), growth.quality, growth.missing, "boolean")


# Backward-friendly names used by callers and tests.
fcf = free_cash_flow
pe_pb_percentile = historical_percentile
consecutive_dividend_years = dividend_years
