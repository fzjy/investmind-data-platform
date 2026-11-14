from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from datetime import date, datetime
from app.shared.payload_validation import _is_number, _matches, _at, _validate_record_list


def _valid_etf_details(etf: object) -> bool:
    if not isinstance(etf, Mapping):
        return False
    try:
        if (not isinstance(etf.get('code'), str) or not isinstance(etf.get('source'), str) or not etf['source'].strip()
            or not all(_is_number(etf.get(k)) for k in ('close','shares','volume'))
            or etf['close'] <= 0 or etf['shares'] <= 0 or etf['volume'] < 0):
            return False
        day = etf['tradeDate']
        if date.fromisoformat(day).isoformat() != day:
            return False
        captured = etf['capturedAt']
        return isinstance(captured, Mapping) and all(
            isinstance(captured.get(k), str) and datetime.fromisoformat(captured[k]).utcoffset() is not None
            for k in ('quote','shares'))
    except (KeyError,ValueError,TypeError,AttributeError):
        return False



def _validate_market_explorer(payload: Mapping[str, Any], errors: list[str]) -> None:
    if 'commodityMetadata' in payload or any(isinstance(row,Mapping) and row.get('dataKind')=='futures-ohlc'
        for group in payload.get('commodityData',{}).values() if isinstance(group,Mapping) for row in group.get('domestic',[])):
        from app.market.commodities.dto import valid_commodity_payload
        if not valid_commodity_payload(payload): errors.append('commodityData.real_ohlc')
    nullable_number = (_is_number, type(None))
    category_ids = {"etf", "majorIndustry", "subIndustry", "region", "concept"}
    categories = payload.get("heatmapCategories")
    _validate_record_list(
        categories,
        "heatmapCategories",
        {"id": str, "label": str, "total": _is_number},
        errors,
    )
    if isinstance(categories, list):
        for index, category in enumerate(categories):
            if isinstance(category, Mapping) and category.get("id") not in category_ids:
                errors.append(f"heatmapCategories[{index}].id")
    heatmap = payload.get("heatmapData")
    if not isinstance(heatmap, Mapping):
        errors.append("heatmapData")
    else:
        for category, sectors in heatmap.items():
            _validate_record_list(
                sectors,
                f"heatmapData.{category}",
                {
                    "id": str,
                    "name": str,
                    "change": nullable_number,
                    "marketCap": nullable_number,
                    "stockCount": nullable_number,
                    "stocks": list,
                    "turnover": nullable_number,
                },
                errors,
            )
            if isinstance(sectors, list):
                for sector_index, sector in enumerate(sectors):
                    if not isinstance(sector, Mapping):
                        continue
                    if sector.get('assetKind') == 'etf':
                        etf = sector.get('etf')
                        if (category != 'etf' or sector.get('stockCount') is not None or sector.get('stocks') != []
                            or not _valid_etf_details(etf)):
                            errors.append(f'heatmapData.{category}[{sector_index}].etf')
                    elif not _is_number(sector.get('stockCount')):
                        errors.append(f'heatmapData.{category}[{sector_index}].stockCount')
                    _validate_record_list(
                        sector.get("stocks"),
                        f"heatmapData.{category}[{sector_index}].stocks",
                        {
                            "symbol": str,
                            "name": str,
                            "price": _is_number,
                            "change": nullable_number,
                            "marketCap": nullable_number,
                            "turnover": nullable_number,
                        },
                        errors,
                    )
    ranking_fields = {"symbol": str, "name": str, "price": _is_number, "change": _is_number}
    _validate_record_list(payload.get("rankingLeaders"), "rankingLeaders", ranking_fields, errors)
    _validate_record_list(payload.get("rankingDecliners"), "rankingDecliners", ranking_fields, errors)



def _validate_market_overview(payload: Mapping[str, Any], errors: list[str]) -> None:
    sentiment = payload.get('sentimentSnapshot')
    if isinstance(sentiment, Mapping) and 'algorithm' in sentiment:
        from app.market.sentiment import SentimentSnapshot
        from pydantic import ValidationError
        try:
            SentimentSnapshot.model_validate(sentiment)
        except (ValidationError, ValueError, TypeError):
            errors.append('sentimentSnapshot')
    for field in ('globalIndices','etfQuotes'):
        required = {'code':str,'name':str,'change':(_is_number,type(None))}
        if field == 'globalIndices':
            required.update(value=str,market=str,intraday=(list,type(None)))
        _validate_record_list(payload.get(field),field,required,errors)
        for index, quote in enumerate(payload.get(field, []) if isinstance(payload.get(field),list) else []):
            if not isinstance(quote,Mapping):
                continue
            if any(key in quote for key in ('tradeDate','capturedAt','source','calendarStatus')):
                for key in ('tradeDate','capturedAt','source','calendarStatus'):
                    if not isinstance(quote.get(key),str) or not quote.get(key):
                        errors.append(f'{field}[{index}].{key}')
    checks = {
        "marketSnapshot.asOf": str,
        "turnoverSnapshot.value": str,
        "limitStats.riseLimit": (_is_number, type(None)),
        "limitStats.fallLimit": (_is_number, type(None)),
        "sentimentSnapshot.score": (_is_number, type(None)),
    }
    for path, expected in checks.items():
        value = _at(payload, *path.split("."))
        if not _matches(value, expected):
            errors.append(path)
