from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from math import isfinite
from typing import Any

from app.stocks.api.financial import CoreSnapshot
from app.marts.contracts import MartCandidate
from app.content.api.read_models import ContentMartBuilder
from app.market.marts.region_heatmap import legacy_regions, region_groups
from app.market.commodities.missing import reconcile_commodity_missing
from app.shared.stock_mapping import StockMappingPolicy, classification_source
from app.market.sentiment import SentimentOptions, build_sentiment


def _number(value: object) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_of_datetime(as_of: date) -> datetime:
    return datetime.combine(as_of, time.min, timezone.utc)


class MarketMartBuilder:
    def __init__(self, content: ContentMartBuilder | None = None, *, limit_statuses: dict[str, int] | None = None, regions: dict | None = None, etf_tiles: list | None = None, commodity_package: dict | None = None, sentiment: SentimentOptions | None = None) -> None:
        if sentiment is None:
            from app.market.data.catalog import MarketDataCatalog
            sentiment = MarketDataCatalog.load().sentiment
        self._sentiment = sentiment
        self._content = content
        self._classification = StockMappingPolicy.load().classification
        self._limit_statuses = limit_statuses
        self._regions = regions
        self._etf_tiles = etf_tiles
        self._commodity_package = commodity_package

    def build(
        self,
        snapshot: CoreSnapshot,
        version: str,
    ) -> tuple[MartCandidate, MartCandidate]:
        prices_by_symbol: dict[str, list[object]] = {}
        for row in snapshot.daily_prices:
            prices_by_symbol.setdefault(row.ts_code, []).append(row)
        common_dates = [
            {row.trade_date for row in rows}
            for rows in prices_by_symbol.values()
            if rows
        ]
        common = set.intersection(*common_dates) if common_dates else set()
        latest_date = max(common) if common else max(
            (row.trade_date for row in snapshot.daily_prices),
            default=snapshot.as_of,
        )
        latest_prices = [
            row for row in snapshot.daily_prices if row.trade_date == latest_date
        ]
        latest_basics = [
            row for row in snapshot.daily_basics if row.trade_date == latest_date and row.ts_code in {price.ts_code for price in latest_prices}
        ]
        security_names = {row.ts_code: row.name for row in snapshot.securities}
        turnover_values = [row.amount for row in latest_prices if row.amount is not None]
        turnover = sum(turnover_values, Decimal("0")) if turnover_values else None
        leaders = sorted(
            (
                {
                    "symbol": row.ts_code,
                    "name": security_names.get(row.ts_code, row.ts_code),
                    "change": _number(row.pct_chg),
                    "price": _number(row.close),
                }
                for row in latest_prices
                if row.pct_chg is not None
            ),
            key=lambda item: float(item["change"] or 0),
            reverse=True,
        )
        decliners = list(reversed(leaders))
        advances = sum(1 for row in latest_prices if row.pct_chg is not None and row.pct_chg > 0)
        declines = sum(1 for row in latest_prices if row.pct_chg is not None and row.pct_chg < 0)
        flat = sum(1 for row in latest_prices if row.pct_chg == 0)
        limit_stats = {"up": 0, "down": 0, "flat": 0}
        for row in latest_basics:
            status = (self._limit_statuses.get(row.ts_code) if self._limit_statuses is not None else
                None if '_limitStatusLineage' in (row.data or {}) else (row.data or {}).get("limit_status"))
            status_code = int(str(status)) if not isinstance(status, bool) and str(status) in {str(code) for code in range(7)} else None
            if status_code in {2, 3}:
                limit_stats["up"] += 1
            elif status_code in {5, 6}:
                limit_stats["down"] += 1
            elif status_code is not None:
                limit_stats["flat"] += 1

        missing = tuple(
            {"source": source, "reason": "尚未采集真实来源"}
            for source in ("indices", "etfs", "fundFlow", "commodities")
        )
        missing = reconcile_commodity_missing(missing, self._commodity_package)
        total = len(latest_prices)
        known_changes = [row for row in latest_prices if row.pct_chg is not None]
        limit_coverage = sum(limit_stats.values())
        turnover_text = "--" if turnover is None else format(turnover, "f")
        overview = {
            "marketSnapshot": {
                "asOf": latest_date.isoformat(),
                "delayLabel": "日线收盘数据",
                "aggregationLabel": "Database 时点聚合",
                "support": "当前覆盖数据库中截至该交易日有可见行情的 A 股证券",
            },
            "marketStatuses": [],
            "globalIndices": [],
            "turnoverSnapshot": {
                "value": turnover_text,
                "unit": "千元",
                "title": "A 股成交额",
                "explanation": f"截至 {latest_date.isoformat()} 的可见日线成交额合计",
            },
            "turnoverTop10": [
                {
                    "rank": index,
                    "code": row.ts_code,
                    "name": security_names.get(row.ts_code, row.ts_code),
                    "turnover": format(row.amount, "f"),
                    "change": _number(row.pct_chg),
                }
                for index, row in enumerate(
                    sorted(
                        (row for row in latest_prices if row.amount is not None),
                        key=lambda item: float(item.amount or 0),
                        reverse=True,
                    )[:10],
                    start=1,
                )
            ],
            "limitStats": {
                "riseLimit": limit_stats["up"] if limit_coverage else None,
                "fallLimit": limit_stats["down"] if limit_coverage else None,
                "coverage": {"valid": limit_coverage, "total": total},
                "status": "缺失" if not limit_coverage else "部分覆盖" if limit_coverage < total else "已收盘",
                "explanation": f"按 Tushare daily_basic limit_status 2/3 与 5/6 聚合；有效覆盖 {limit_coverage}/{total}，缺失不计作零",
            },
            "sentimentSnapshot": build_sentiment(snapshot.daily_prices, snapshot.securities, latest_date, self._sentiment),
            "fundFlowSnapshot": {"status": "缺失", "northbound": "--", "mainForce": "--"},
            "breadthSnapshots": [{
                "market": "A股",
                "sentiment": "偏强" if advances > declines else "偏弱" if declines > advances else "中性",
                "rise": advances,
                "fall": declines,
                "averageChange": round(sum(float(row.pct_chg) for row in known_changes) / len(known_changes), 2) if known_changes else None,
                "total": total,
                "refreshLabel": latest_date.isoformat(),
                "riseWidthClass": "w-1/2",
            }],
            "etfQuotes": [],
            "missingData": list(missing),
        }
        latest_price_by_symbol = {
            row.ts_code: row
            for row in latest_prices
        }
        latest_basic_by_symbol = {
            row.ts_code: row
            for row in latest_basics
        }
        sectors_by_category: dict[str, dict[str, dict[str, object]]] = {}
        classifications = {row.id: row for row in snapshot.classifications
                           if row.provider == self._classification.provider and classification_source(row) == self._classification.source}
        classification_keys = {(row.provider, classification_source(row), row.classification_id): row for row in classifications.values()}
        expanded_members: dict[tuple[int, str], object] = {}
        for member in snapshot.classification_members:
            if member.effective_from > latest_date or (member.effective_to is not None and member.effective_to < latest_date):
                continue
            classification = classifications.get(member.classification_id)
            visited: set[int] = set()
            while classification is not None and classification.id not in visited:
                visited.add(classification.id)
                expanded_members[(classification.id, member.ts_code)] = member
                classification = classification_keys.get((classification.provider, classification_source(classification), classification.parent_id))
        for (classification_key, _symbol), member in expanded_members.items():
            classification = classifications.get(classification_key)
            if classification is None:
                continue
            if classification.level not in {self._classification.majorLevel, self._classification.subLevel}:
                continue
            price = latest_price_by_symbol.get(member.ts_code)
            if price is None or price.close is None:
                continue
            basic = latest_basic_by_symbol.get(member.ts_code)
            category_id = "majorIndustry" if classification.level == self._classification.majorLevel else "subIndustry"
            category = sectors_by_category.setdefault(category_id, {})
            sector = category.setdefault(
                classification.classification_id,
                {
                    "id": classification.classification_id,
                    "name": classification.name,
                    "stocks": [],
                },
            )
            stocks = sector["stocks"]
            assert isinstance(stocks, list)
            stocks.append(
                {
                    "symbol": member.ts_code,
                    "name": security_names.get(member.ts_code, member.ts_code),
                    "price": _number(price.close),
                    "change": _number(price.pct_chg),
                    "marketCap": _number(basic.total_mv) / 10000 if basic is not None and _number(basic.total_mv) is not None else None,
                    "turnover": _number(price.amount) / 100000 if _number(price.amount) is not None else None,
                }
            )

        sectors_by_category['region'], region_metadata = region_groups(latest_prices, latest_basics,
            security_names, self._regions if self._regions is not None else legacy_regions(snapshot.securities), latest_date)
        category_labels = {
            "majorIndustry": "大类行业",
            "subIndustry": "细分行业",
            "region": "地域",
        }
        heatmap_categories: list[dict[str, object]] = []
        heatmap_data: dict[str, list[dict[str, object]]] = {}
        for category_id in ("majorIndustry", "subIndustry", "region"):
            raw_sectors = sectors_by_category.get(category_id)
            if not raw_sectors:
                continue
            sectors: list[dict[str, object]] = []
            for raw_sector in raw_sectors.values():
                stocks = raw_sector["stocks"]
                assert isinstance(stocks, list)
                stocks.sort(key=lambda item: str(item["symbol"]))
                stock_count = len(stocks)
                changes = [item["change"] for item in stocks if item["change"] is not None]
                caps = [item["marketCap"] for item in stocks if item["marketCap"] is not None]
                amounts = [item["turnover"] for item in stocks if item["turnover"] is not None]
                coverage = {"change": len(changes), "marketCap": len(caps), "turnover": len(amounts), "total": stock_count}
                sectors.append(
                    {
                        **raw_sector,
                        "change": round(sum(changes) / len(changes), 4) if changes else None,
                        "marketCap": sum(caps) if len(caps) == stock_count else None,
                        "stockCount": stock_count,
                        "turnover": sum(amounts) if len(amounts) == stock_count else None,
                        **({"coverage": coverage} if any(coverage[key] < stock_count for key in ("change", "marketCap", "turnover")) else {}),
                    }
                )
            sectors.sort(key=lambda item: str(item["id"]))
            heatmap_categories.append(
                {
                    "id": category_id,
                    "label": category_labels[category_id],
                    "total": len(sectors),
                }
            )
            heatmap_data[category_id] = sectors
        explorer = {
            "amountUnit": "yi-yuan",
            "commodityCategories": [],
            "commodityData": {},
            "commodityPeriods": [],
            "heatmapCategories": heatmap_categories,
            "heatmapData": heatmap_data,
            "regionMetadata": region_metadata,
            "marketNews": self._content.build(_as_of_datetime(snapshot.as_of)) if self._content else [],
            "rankingDecliners": decliners[:10],
            "rankingLeaders": leaders[:10],
            "snapshotLabel": latest_date.isoformat(),
            "missingData": list(missing),
        }
        if self._etf_tiles:
            from app.market.etf.publication import overlay
            explorer = overlay(explorer, self._etf_tiles)
        if self._commodity_package:
            explorer = {**explorer, **self._commodity_package}
        return (
            MartCandidate("market-overview", _as_of_datetime(snapshot.as_of), overview, missing_data=missing),
            MartCandidate("market-explorer", _as_of_datetime(snapshot.as_of), explorer, missing_data=missing),
        )

    def build_explorer(self, as_of: datetime) -> dict[str, Any]:
        return {
            "commodityCategories": [],
            "commodityData": {},
            "commodityPeriods": [],
            "heatmapCategories": [],
            "heatmapData": {},
            "marketNews": self._content.build(as_of) if self._content else [],
            "rankingDecliners": [],
            "rankingLeaders": [],
            "snapshotLabel": as_of.isoformat(),
            "missingData": [],
        }
