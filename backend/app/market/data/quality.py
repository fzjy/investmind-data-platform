"""各市场报价独立时点；不以较旧股票统计日期审判新报价。"""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from app.marts.contracts import QualityContext, QualityResult
from app.marts.dto import validate_product_payload
from app.marts.quality import QualityGate
from app.market.data.catalog import plan_market_targets


def clear_resolved_quote_missing(candidates, *, catalog):
    """完整真实报价组解决的是两份市场快照共同的来源缺失，不清除其他来源。"""
    overview = next((item.payload for item in candidates if item.product == 'market-overview'), {})
    resolved = set()
    for field, source in (('globalIndices', 'indices'), ('etfQuotes', 'etfs')):
        assets = {asset.asset_code: asset for asset in catalog.assets if asset.asset_type == source}
        quotes = overview.get(field, [])
        if len(quotes) == len(assets) and {quote.get('code') for quote in quotes} == set(assets) and all(
            quote.get('tradeDate') and quote.get('capturedAt')
            and quote.get('source') == f'tushare:{assets[quote["code"]].api_name}'
            and quote.get('calendarStatus') == 'unknown' for quote in quotes
        ):
            resolved.add(source)
    return [replace(candidate,
        payload={**candidate.payload, 'missingData': [row for row in candidate.payload.get('missingData', [])
            if row.get('source') not in resolved]},
        missing_data=tuple(row for row in candidate.missing_data if row.get('source') not in resolved))
        for candidate in candidates]


def evaluate_market_quality(candidates, *, checked_at: datetime, catalog) -> tuple[QualityResult, ...]:
    if checked_at.utcoffset() is None:
        raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
    targets = {t.asset.asset_code: t for t in plan_market_targets(catalog, checked_at)}
    valid = True
    dto_errors, clean = [], []
    for candidate in candidates:
        dto_errors.extend(validate_product_payload(candidate.product, candidate.payload))
        payload = candidate.payload
        if candidate.product == 'market-overview':
            for group in ('globalIndices','etfQuotes'):
                seen = set()
                for quote in payload.get(group, []):
                    # 原Fixture/无本批元数据的旧报价仍可读；新报价必须完整。
                    if not any(key in quote for key in ('tradeDate','capturedAt','source','calendarStatus')):
                        continue
                    try:
                        code = quote['code']
                        target = targets[code]
                        expected_group = 'globalIndices' if target.asset.asset_type == 'indices' else 'etfQuotes'
                        day = date.fromisoformat(quote['tradeDate'])
                        captured = datetime.fromisoformat(quote['capturedAt'])
                        change = quote['change']
                        valid = valid and code not in seen and group == expected_group
                        # 旧的真实报价可以保留，不能因回看窗口缩短变成未来或伪造数据。
                        valid = valid and day <= target.end_date
                        valid = valid and captured.utcoffset() is not None and captured <= checked_at
                        valid = valid and quote['source'] == f'tushare:{target.asset.api_name}'
                        valid = valid and quote['calendarStatus'] == 'unknown'
                        valid = valid and (change is None or isinstance(change, (int,float)) and not isinstance(change,bool) and isfinite(change))
                        if group == 'globalIndices':
                            price = Decimal(quote['value'].replace(',', ''))
                            valid = valid and price.is_finite() and price > 0 and quote['intraday'] is None
                            valid = valid and quote['market'] == target.asset.market
                        seen.add(code)
                    except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError):
                        valid = False
            payload = {**payload, 'globalIndices': [], 'etfQuotes': []}
        elif candidate.product == 'market-explorer':
            for item in payload.get('marketNews', []):
                try:
                    instant = datetime.fromisoformat(item['publishedAt'])
                    valid = valid and instant.utcoffset() is not None and instant <= checked_at
                except (ValueError, TypeError, KeyError):
                    valid = False
            payload = {**payload, 'marketNews': []}
        clean.append(replace(candidate, payload=payload))
    checks = QualityGate().evaluate(clean, QualityContext(0, (), 0, 0, ()))
    return (*checks, QualityResult('market-independent-time-and-quotes', 'blocking', bool(valid and not dto_errors),
        {'dtoErrors': dto_errors}))
