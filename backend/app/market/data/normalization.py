from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.connectors.tushare.contracts import TushareResponse
from app.market.data.contracts import MarketTarget, NormalizedMarketQuote, QUOTE_FIELDS


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError('MARKET_NUMBER_INVALID')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('MARKET_NUMBER_INVALID') from exc
    if not result.is_finite() or abs(result) >= Decimal('1000000000000'):
        raise ValueError('MARKET_NUMBER_INVALID')
    return result


def normalize_market_response(target: MarketTarget, response: TushareResponse, *,
    pct_tolerance: Decimal) -> tuple[NormalizedMarketQuote, ...]:
    if not pct_tolerance.is_finite() or not 0 <= pct_tolerance <= Decimal('0.1'):
        raise ValueError('MARKET_TOLERANCE_INVALID')
    if len(set(response.fields)) != len(response.fields) or not set(QUOTE_FIELDS) <= set(response.fields):
        raise ValueError('MARKET_FIELDS_INVALID')
    if not response.items or len(response.items) > (target.end_date - target.start_date).days + 1:
        raise ValueError('MARKET_WINDOW_INCOMPLETE')
    values = {}
    for row in response.rows():
        if row['ts_code'] != target.asset.asset_code:
            raise ValueError('MARKET_IDENTITY_MISMATCH')
        text = row['trade_date']
        if not isinstance(text, str) or re.fullmatch(r'\d{8}', text) is None:
            raise ValueError('MARKET_DATE_INVALID')
        day = datetime.strptime(text, '%Y%m%d').date()
        if not target.start_date <= day <= target.end_date:
            raise ValueError('MARKET_DATE_OUTSIDE_WINDOW')
        op, high, low, close, pre, pct = (_number(row[key]) for key in QUOTE_FIELDS[2:])
        if min(op, high, low, close, pre) <= 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError('MARKET_PRICE_INVALID')
        if abs((close / pre - 1) * 100 - pct) > pct_tolerance:
            raise ValueError('MARKET_CHANGE_MISMATCH')
        quote = NormalizedMarketQuote(target.asset, day, op, high, low, close, pre, pct)
        if day in values and values[day] != quote:
            raise ValueError('MARKET_CONFLICTING_ROWS')
        values[day] = quote
    return tuple(values[day] for day in sorted(values))
