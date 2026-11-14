from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import Any
from app.shared.payload_validation import _is_number, _matches, _at, _validate_record_list
from app.stocks.screener_contracts import LIST_RISK_KEYS
from app.stocks.rea_fields import REA_BOARDS as BOARD_OPTIONS, REA_EXCLUSIONS as EXCLUSION_OPTIONS, REA_THRESHOLD_UI as THRESHOLD_DEFINITIONS, REA_GROUPS as FILTER_GROUPS, REA_PRESETS as PRESET_DEFINITIONS, REA_SORT_FIELDS as SORT_FIELDS


CATEGORY_OPTIONS = [{"id":key,"label":label} for key,label in (("majorIndustry","大类行业"),("subIndustry","细分行业"),("concept","概念"),("region","地域"))]



def screener_bootstrap(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def unique(field: str) -> list[str]:
        return sorted({str(row[field]) for row in rows if row.get(field)})

    concepts = sorted({str(value) for row in rows for value in row.get("concepts", [])})
    return {
        "presets": PRESET_DEFINITIONS,
        "exclusions": EXCLUSION_OPTIONS,
        "boards": BOARD_OPTIONS,
        "categories": CATEGORY_OPTIONS,
        "categoryValues": {
            "majorIndustry": unique("majorIndustry"),
            "subIndustry": unique("subIndustry"),
            "concept": concepts,
            "region": unique("region"),
        },
        "groups": FILTER_GROUPS,
        "thresholds": THRESHOLD_DEFINITIONS,
        "sortFields": SORT_FIELDS,
        "totalCount": len(rows),
        "evidenceBadge": "Database 时点快照 · 机构部分统计",
    }



def _validate_workbench(payload: Mapping[str, Any], errors: list[str]) -> None:
    quote = _at(payload, "quote")
    if isinstance(quote, Mapping):
        for field, expected in {
            "symbol": str,
            "name": str,
            "market": str,
            "industry": str,
            "subIndustry": str,
            "mentions": _is_number,
            "latestMention": str,
            "price": _is_number,
            "changePercent": (_is_number, type(None)),
            "previousClose": _is_number,
        }.items():
            if field not in quote or not _matches(quote[field], expected):
                errors.append(f"quote.{field}")
        for field in ("pe", "pb"):
            if field not in quote or (
                quote[field] is not None and not _is_number(quote[field])
            ):
                errors.append(f"quote.{field}")
        if not isinstance(quote.get("concepts"), list):
            errors.append("quote.concepts")
    _validate_record_list(
        _at(payload, "sidebar", "stocks"),
        "sidebar.stocks",
        {
            "symbol": str,
            "name": str,
            "pinyin": str,
            "price": _is_number,
            "changePercent": (_is_number, type(None)),
            "mentions": _is_number,
        },
        errors,
    )
    _validate_record_list(
        _at(payload, "dailyChart", "candles"),
        "dailyChart.candles",
        {
            "date": str,
            "open": _is_number,
            "high": _is_number,
            "low": _is_number,
            "close": _is_number,
            "volume": _is_number,
        },
        errors,
    )
    peg = _at(payload, "financial", "dashboard", "peg")
    if not isinstance(peg, Mapping):
        errors.append("financial.dashboard.peg")
    else:
        for field in ("value", "pe", "growth"):
            if field not in peg or (
                peg[field] is not None and not _is_number(peg[field])
            ):
                errors.append(f"financial.dashboard.peg.{field}")



def _validate_screener(payload: Mapping[str, Any], errors: list[str]) -> None:
    nullable_number = (_is_number, type(None))
    _validate_record_list(
        payload.get("stocks"),
        "stocks",
        {
            "symbol": str,
            "name": str,
            "price": _is_number,
            "change": nullable_number,
            "marketCap": nullable_number,
            "pe": nullable_number,
            "pb": nullable_number,
            "dividendYield": nullable_number,
            "roe": nullable_number,
            "grossMargin": nullable_number,
            "netMargin": nullable_number,
            "revenueGrowth": nullable_number,
            "profitGrowth": nullable_number,
            "realMoneyIndex": nullable_number,
            "cashflowGrowth": nullable_number,
            "dividendYears": nullable_number,
            "payoutRatio": nullable_number,
            "incomeSafety": nullable_number,
            "industry": str,
            "board": str,
            "isST": bool,
            "isLoss": (bool, type(None)),
            "listingYears": _is_number,
            "pePercentile": nullable_number,
            "pbPercentile": nullable_number,
            "expectedDividendYield": nullable_number,
            "capitalReturnRatio": nullable_number,
            "roic": nullable_number,
            "institutionHolding": nullable_number,
            "majorIndustry": str,
            "subIndustry": str,
            "concepts": list,
            "region": str,
            "inventoryRisk": (bool, type(None)),
            "netDebt": (bool, type(None)),
        },
        errors,
    )
    for i, row in enumerate(payload['stocks'] if isinstance(payload.get('stocks'), list) else []):
        if not isinstance(row, Mapping):
            continue
        if 'listRisks' in row:
            risks = row['listRisks']
            if (not isinstance(risks, list) or len(risks) != len(LIST_RISK_KEYS) or
                    any(not isinstance(r, Mapping) or r.get('key') != key or
                        r.get('status') not in ('unknown','normal','warn','danger') or
                        not all(isinstance(r.get(k), str) and r[k].strip() for k in ('period','detail'))
                        for key,r in zip(LIST_RISK_KEYS, risks))):
                errors.append(f'stocks[{i}].listRisks')
        if 'listMissingReasons' in row:
            reasons = row['listMissingReasons']
            if not isinstance(reasons, Mapping) or not all(isinstance(k,str) and isinstance(v,str) and v.strip() for k,v in reasons.items()):
                errors.append(f'stocks[{i}].listMissingReasons')
    _validate_record_list(
        _at(payload, "bootstrap", "presets"),
        "bootstrap.presets",
        {"id": str, "label": str, "tone": str, "patch": Mapping},
        errors,
    )
