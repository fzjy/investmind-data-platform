from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from app.db.models import DailyPrice
from app.stocks.api.financial import CoreSnapshot, years_before
from app.marts.contracts import MartCandidate
from app.stocks.api.quotes import adjusted_value
from app.portfolio.marts.factor_coverage import factor_coverage, index_portfolio_factors


def _number(value: object) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _as_of_datetime(as_of: date) -> datetime:
    return datetime.combine(as_of, time.min, timezone.utc)


class PortfolioMartBuilder:
    def __init__(self, session_factory=None):
        self._session_factory = session_factory

    def build(self, snapshot: CoreSnapshot, version: str) -> MartCandidate:
        observations, adjustment_index = index_portfolio_factors(snapshot)
        coverage = {}
        prices_by_symbol: dict[str, list[DailyPrice]] = {}
        for row in snapshot.daily_prices:
            prices_by_symbol.setdefault(row.ts_code, []).append(row)
        prices: dict[str, list[dict[str, object]]] = {}
        unadjusted_prices: dict[str, list[dict[str, object]]] = {}
        assets: list[dict[str, object]] = []
        for security in snapshot.securities:
            rows = prices_by_symbol.get(security.ts_code, [])
            if not rows:
                continue
            coverage[security.ts_code] = factor_coverage(rows, observations.get(security.ts_code, {}))
            assets.append(
                {
                    "code": security.ts_code,
                    "id": security.ts_code,
                    "name": security.name,
                    "symbol": security.symbol,
                    "type": "stock",
                    "currency": "CNY",
                    **({"listDate": security.list_date.isoformat()} if security.list_date else {}),
                }
            )
            prices[security.ts_code] = [
                {
                    "date": row.trade_date.isoformat(),
                    "price": _number(adjusted_value(adjustment_index, security.ts_code, row.trade_date, row.close)),
                }
                for row in rows
            ]
            unadjusted_prices[security.ts_code] = [
                {"date": row.trade_date.isoformat(), "price": _number(row.close)}
                for row in rows
            ]
        assets.append(
            {
                "code": "CNY",
                "id": "CNY",
                "name": "人民币现金",
                "symbol": "CNY",
                "type": "cash",
                "currency": "CNY",
            }
        )
        prices["CNY"] = [{"date": snapshot.as_of.isoformat(), "price": 1}]
        payload = {
            "assets": assets,
            "prices": prices,
            "factorCoverage": coverage,
            "unadjustedPrices": {**unadjusted_prices, "CNY": [{"date": snapshot.as_of.isoformat(), "price": 1}]},
            "snapshotLabel": snapshot.as_of.isoformat(),
        }
        if self._session_factory is not None:
            from app.portfolio.marts import portfolio_ledger
            symbols = tuple(asset['id'] for asset in assets if asset['type'] == 'stock')
            starts = {symbol:min(p['date'] for p in unadjusted_prices[symbol]) for symbol in symbols}
            lookbacks = {symbol: years_before(date.fromisoformat(day), 1).isoformat() for symbol, day in starts.items()}
            payload['dividendLedger'] = portfolio_ledger.load_portfolio_dividend_ledger(
                self._session_factory, symbols, snapshot.as_of, lookbacks)
            from app.portfolio.dividend_yield_inputs import load_yield_inputs
            payload['dividendYieldInputs'] = load_yield_inputs(self._session_factory, starts, snapshot.as_of, payload['dividendLedger'])
            from app.portfolio.attribution_inputs import load_attribution_inputs
            payload['attributionInputs'] = load_attribution_inputs(self._session_factory, starts, snapshot.as_of)
            from app.portfolio.treasury import load_treasury_reference
            if starts:
                payload['treasuryReference'] = load_treasury_reference(self._session_factory, snapshot.as_of, min(starts.values()))
                from app.portfolio.reference_history import load_benchmarks
                payload['benchmarks'] = load_benchmarks(self._session_factory,snapshot.as_of,min(starts.values()))
        return MartCandidate("portfolio", _as_of_datetime(snapshot.as_of), payload)


__all__ = ["PortfolioMartBuilder"]
