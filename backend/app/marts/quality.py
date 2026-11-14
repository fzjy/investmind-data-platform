from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from math import isfinite
import re
from typing import Iterable

from app.marts.contracts import MartCandidate, QualityContext, QualityResult
from app.marts.consistency import row_consistency
from app.marts.dto import validate_product_payload
from app.stocks.screener_contracts import SCREENER_FIELDS


REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "market-overview": frozenset(
        {
            "marketSnapshot",
            "marketStatuses",
            "globalIndices",
            "turnoverSnapshot",
            "turnoverTop10",
            "limitStats",
            "sentimentSnapshot",
            "fundFlowSnapshot",
            "breadthSnapshots",
            "etfQuotes",
        }
    ),
    "market-explorer": frozenset(
        {
            "commodityCategories",
            "commodityData",
            "commodityPeriods",
            "heatmapCategories",
            "heatmapData",
            "marketNews",
            "rankingDecliners",
            "rankingLeaders",
            "snapshotLabel",
        }
    ),
    "macro-transmission": frozenset({"transmission", "analysis"}),
    "macro-insights": frozenset(
        {
            "compassPoints",
            "sandboxGroups",
            "sandboxIndicators",
            "sandboxMonths",
            "sandboxPresets",
            "statusCards",
            "trendDefinitions",
        }
    ),
    "screener": frozenset({"bootstrap", "stocks"}),
    "portfolio": frozenset(
        {"assets", "defaultState", "prices", "snapshotLabel", "storageKey"}
    ),
    "stock-workbench": frozenset(
        {
            "symbol",
            "sidebar",
            "quote",
            "dailyChart",
            "metrics",
            "risks",
            "financial",
            "isFallbackSample",
            "dataAvailability",
        }
    ),
}


def _base_product(product: str) -> str:
    return product.split(":", 1)[0]


def _date_from_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _future_dates(value: object, as_of: date, key: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.extend(_future_dates(child, as_of, str(child_key)))
    elif isinstance(value, (tuple, list)):
        for child in value:
            found.extend(_future_dates(child, as_of, key))
    elif key.lower() in {
        "date",
        "tradedate",
        "enddate",
        "ann_date",
        "anndate",
        "f_ann_date",
        "fanndate",
        "observedat",
        "publishedat",
        "announcementdate",
        "asof",
        "referencedate",
    }:
        parsed = _date_from_value(value)
        if parsed is not None and parsed > as_of:
            found.append(str(value))
    return found


def _ohlc_rows(candidates: Iterable[MartCandidate]) -> Iterable[dict[str, object]]:
    for candidate in candidates:
        yield from candidate.rows.charts
        chart = candidate.payload.get("dailyChart")
        if isinstance(chart, dict):
            candles = chart.get("candles")
            if isinstance(candles, list):
                yield from (item for item in candles if isinstance(item, dict))


def _ohlc_diagnostics(values: dict[str, object]) -> dict[str, object]:
    # 所有失败分支都要能记录；保留正常JSON标量，Decimal/非有限/复杂坏值仅作文本诊断。
    return {
        name: value if value is None or isinstance(value, (str, bool, int))
        or isinstance(value, float) and isfinite(value) else str(value)
        for name, value in values.items()
    }


class QualityGate:
    def evaluate(
        self,
        candidates: tuple[MartCandidate, ...] | list[MartCandidate],
        context: QualityContext,
    ) -> tuple[QualityResult, ...]:
        items = tuple(candidates)
        results: list[QualityResult] = []
        results.append(row_consistency(items))
        results.extend(context.source_quality_results)
        missing_keys: dict[str, list[str]] = {}
        for candidate in items:
            required = REQUIRED_KEYS.get(_base_product(candidate.product), frozenset())
            missing = sorted(required - candidate.payload.keys())
            missing.extend(validate_product_payload(candidate.product, candidate.payload))
            if missing:
                missing_keys[candidate.product] = sorted(set(missing))
        results.append(
            QualityResult(
                "dto-keys",
                "blocking",
                not missing_keys,
                {"missing": missing_keys},
            )
        )

        screener = next(
            (candidate for candidate in items if candidate.product == "screener"),
            None,
        )
        stock_rows = screener.payload.get("stocks") if screener else None
        if context.expected_screener_symbols is not None:
            expected = set(context.expected_screener_symbols)
            actual = {row.get("symbol") for row in stock_rows if isinstance(row, dict)
                      and isinstance(row.get("symbol"), str)} if isinstance(stock_rows, list) else set()
            results.append(QualityResult(
                "screener-expected-scope", "blocking", expected == actual,
                {"expectedCount": len(expected), "actualCount": len(actual),
                 "missing": sorted(expected - actual), "unexpected": sorted(actual - expected)}))
        fields = tuple(sorted(SCREENER_FIELDS))
        coverage = {field: {"present": sum(row.get(field) is not None for row in stock_rows if isinstance(row, dict)),
                            "total": len(stock_rows)} for field in fields} if isinstance(stock_rows, list) else {}
        results.append(QualityResult("screener-field-coverage", "advisory",
                                     all(value["present"] == value["total"] for value in coverage.values()),
                                     {"fields": coverage}))
        if context.baseline_screener_rows is not None and isinstance(stock_rows, list):
            baseline = {row["symbol"]: row for row in context.baseline_screener_rows}
            current = {row["symbol"]: row for row in stock_rows if isinstance(row, dict)
                       and isinstance(row.get("symbol"), str) and row["symbol"] in baseline}
            row_drop = (len(baseline) - len(current)) / len(baseline) if baseline else 0.0
            results.append(QualityResult("screener-record-count-stable", "blocking",
                row_drop <= context.max_row_drop_ratio,
                {"baselineCount": len(baseline), "retainedCount": len(current),
                 "dropRatio": row_drop, "maxDropRatio": context.max_row_drop_ratio}))
            drops = {}
            for field in fields:
                old_rate = Fraction(sum(row.get(field) is not None for row in baseline.values()), len(baseline) or 1)
                new_rate = Fraction(sum(row.get(field) is not None for row in current.values()), len(current) or 1)
                if old_rate - new_rate > Fraction(str(context.max_field_drop_ratio)):
                    drops[field] = {"baselineRate": float(old_rate), "currentRate": float(new_rate)}
            results.append(QualityResult("screener-field-coverage-stable", "blocking", not drops,
                {"drops": drops, "maxDropRatio": context.max_field_drop_ratio}))
        screener_passed = (
            (not context.expected_screener_symbols if context.expected_screener_symbols is not None
             else context.eligible_security_count == 0)
            or isinstance(stock_rows, list)
            and len(stock_rows) > 0
        )
        results.append(
            QualityResult(
                "screener-not-empty",
                "blocking",
                screener_passed,
                {
                    "eligibleSecurityCount": context.eligible_security_count,
                    "actualCount": len(stock_rows) if isinstance(stock_rows, list) else 0,
                },
            )
        )

        requested = {
            f"stock-workbench:{symbol}" for symbol in context.requested_workbenches
        }
        present = {candidate.product for candidate in items}
        results.append(
            QualityResult(
                "workbench-products",
                "blocking",
                requested <= present,
                {"missing": sorted(requested - present)},
            )
        )

        bad_workbenches: dict[str, list[str]] = {}
        for candidate in items:
            if _base_product(candidate.product) != "stock-workbench":
                continue
            problems: list[str] = []
            quote = candidate.payload.get("quote")
            chart = candidate.payload.get("dailyChart")
            if not isinstance(quote, dict) or quote.get("price") is None:
                problems.append("quote.price")
            candles = chart.get("candles") if isinstance(chart, dict) else None
            if not isinstance(candles, list) or not candles:
                problems.append("dailyChart.candles")
            if problems:
                bad_workbenches[candidate.product] = problems
        results.append(QualityResult(
            "workbench-data",
            "blocking",
            not bad_workbenches,
            {"invalid": bad_workbenches},
        ))

        duplicate_keys: dict[str, list[str]] = {}
        for candidate in items:
            keys: list[str] = []
            keys.extend(
                str(row.get("symbol"))
                for row in candidate.rows.screener
                if row.get("symbol") is not None
            )
            keys.extend(
                str(row.get("symbol"))
                for row in candidate.rows.workbenches
                if row.get("symbol") is not None
            )
            chart_symbol = candidate.product.split(":", 1)[1] if ":" in candidate.product else ""
            keys.extend(
                f"{chart_symbol}:{row.get('date') or row.get('tradeDate') or row.get('trade_date')}"
                for row in candidate.rows.charts
            )
            duplicates_for_product = sorted(key for key in set(keys) if keys.count(key) > 1)
            if duplicates_for_product:
                duplicate_keys[candidate.product] = duplicates_for_product
        results.append(QualityResult(
            "logical-keys-unique",
            "blocking",
            not duplicate_keys,
            {"duplicates": duplicate_keys},
        ))

        invalid_symbols: list[str] = []
        invalid_dates: list[str] = []
        symbol_pattern = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
        for candidate in items:
            for row in (*candidate.rows.screener, *candidate.rows.workbenches):
                symbol = row.get("symbol")
                if symbol is not None and (not isinstance(symbol, str) or not symbol_pattern.fullmatch(symbol)):
                    invalid_symbols.append(str(symbol))
            for row in candidate.rows.charts:
                raw_date = row.get("date") or row.get("tradeDate") or row.get("trade_date")
                if _date_from_value(raw_date) is None:
                    invalid_dates.append(str(raw_date))
        results.append(QualityResult(
            "logical-keys-valid",
            "blocking",
            not invalid_symbols and not invalid_dates,
            {"symbols": sorted(set(invalid_symbols)), "dates": sorted(set(invalid_dates))},
        ))

        portfolio = next((candidate for candidate in items if candidate.product == "portfolio"), None)
        assets = portfolio.payload.get("assets") if portfolio else None
        prices = portfolio.payload.get("prices") if portfolio else None
        stock_assets = [item for item in assets if isinstance(item, dict) and item.get("type") != "cash"] if isinstance(assets, list) else []
        portfolio_passed = portfolio is None or bool(stock_assets) and isinstance(prices, dict) and all(prices.get(str(item.get("id"))) for item in stock_assets)
        results.append(QualityResult(
            "portfolio-has-assets",
            "blocking",
            portfolio_passed,
            {"stockAssetCount": len(stock_assets)},
        ))

        market = next((candidate for candidate in items if candidate.product == "market-overview"), None)
        breadth = market.payload.get("breadthSnapshots") if market else None
        results.append(QualityResult(
            "market-breadth-not-empty",
            "blocking",
            market is None or isinstance(breadth, list) and bool(breadth),
            {"actualCount": len(breadth) if isinstance(breadth, list) else 0},
        ))

        screener_without_price = [
            str(row.get("symbol"))
            for row in stock_rows or []
            if isinstance(row, dict) and row.get("price") is None
        ] if isinstance(stock_rows, list) else []
        results.append(QualityResult(
            "screener-visible-prices",
            "blocking",
            not screener_without_price,
            {"symbols": screener_without_price},
        ))

        as_of_values = {candidate.as_of for candidate in items}
        results.append(QualityResult(
            "shared-as-of",
            "blocking",
            len(as_of_values) <= 1,
            {"values": sorted(value.isoformat() for value in as_of_values)},
        ))

        bad_ohlc: list[dict[str, object]] = []
        for row in _ohlc_rows(items):
            values = {name: row.get(name) for name in ("open", "high", "low", "close")}
            if any(value is None for value in values.values()):
                bad_ohlc.append(_ohlc_diagnostics(values))
                continue
            try:
                opening = Decimal(str(values["open"]))
                high = Decimal(str(values["high"]))
                low = Decimal(str(values["low"]))
                close = Decimal(str(values["close"]))
            except (ArithmeticError, ValueError):
                bad_ohlc.append(_ohlc_diagnostics(values))
                continue
            if not all(value.is_finite() for value in (opening, high, low, close)):
                # NaN 比较会抛异常；Infinity 可能满足关系。先阻断，并保留可写入JSON的详情。
                bad_ohlc.append(_ohlc_diagnostics(values))
                continue
            if high < max(opening, close) or low > min(opening, close) or low > high:
                bad_ohlc.append(_ohlc_diagnostics(values))
        results.append(
            QualityResult("ohlc-valid", "blocking", not bad_ohlc, {"rows": bad_ohlc})
        )

        future = {
            candidate.product: _future_dates(candidate.payload, candidate.as_of.date())
            for candidate in items
        }
        future = {product: values for product, values in future.items() if values}
        results.append(
            QualityResult("no-future-data", "blocking", not future, {"dates": future})
        )

        results.append(
            QualityResult(
                "lineage-complete",
                "blocking",
                context.declared_lineage_count == context.persisted_lineage_count,
                {
                    "declared": context.declared_lineage_count,
                    "persisted": context.persisted_lineage_count,
                },
            )
        )

        products = [candidate.product for candidate in items]
        duplicates = sorted(
            product for product in set(products) if products.count(product) > 1
        )
        results.append(
            QualityResult(
                "unique-products",
                "blocking",
                not duplicates,
                {"duplicates": duplicates},
            )
        )

        results.append(
            QualityResult(
                "source-logical-keys-unique",
                "blocking",
                not context.source_duplicate_keys,
                {"duplicates": list(context.source_duplicate_keys)},
            )
        )

        missing_sources = set(context.missing_sources)
        for candidate in items:
            missing_sources.update(
                str(item.get("source"))
                for item in candidate.missing_data
                if item.get("source")
            )
        results.append(
            QualityResult(
                "external-data-coverage",
                "advisory",
                not missing_sources,
                {"missingSources": sorted(missing_sources)},
            )
        )
        results.append(
            QualityResult(
                "derived-data-coverage",
                "advisory",
                context.declared_lineage_count > 0
                and context.declared_lineage_count == context.persisted_lineage_count,
                {
                    "declared": context.declared_lineage_count,
                    "persisted": context.persisted_lineage_count,
                },
            )
        )
        return tuple(results)


__all__ = ["QualityGate", "QualityContext", "QualityResult"]
