from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping

from app.stocks.derived.inputs import CoreSnapshot


@dataclass(frozen=True)
class AdjustmentSeries:
    dates: tuple[date, ...]
    values: tuple[Decimal, ...]


@dataclass(frozen=True)
class AdjustmentIndex:
    series: Mapping[str, AdjustmentSeries]


def build_adjustment_index(snapshot: CoreSnapshot) -> AdjustmentIndex:
    grouped: dict[str, list[tuple[date, Decimal]]] = {}
    for row in snapshot.adjustment_factors:
        if row.adj_factor is not None:
            grouped.setdefault(row.ts_code, []).append(
                (row.trade_date, row.adj_factor)
            )
    series: dict[str, AdjustmentSeries] = {}
    for symbol, rows in grouped.items():
        rows.sort(key=lambda row: row[0])
        series[symbol] = AdjustmentSeries(
            dates=tuple(row[0] for row in rows),
            values=tuple(row[1] for row in rows),
        )
    return AdjustmentIndex(series)


def adjustment_ratio(
    index: AdjustmentIndex,
    symbol: str,
    trade_date: date,
) -> Decimal:
    factors = index.series.get(symbol)
    if factors is None:
        return Decimal("1")
    row_index = bisect_right(factors.dates, trade_date) - 1
    if row_index < 0:
        return Decimal("1")
    latest_factor = factors.values[-1]
    row_factor = factors.values[row_index]
    if latest_factor == Decimal("0"):
        return Decimal("1")
    return row_factor / latest_factor


def adjusted_value(
    index: AdjustmentIndex,
    symbol: str,
    trade_date: date,
    value: object,
) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return numeric * adjustment_ratio(index, symbol, trade_date)


__all__ = [
    "AdjustmentIndex",
    "adjusted_value",
    "adjustment_ratio",
    "build_adjustment_index",
]
