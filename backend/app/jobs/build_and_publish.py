from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
import json
import re
from typing import Any

from app.stocks.derived.pipeline import DerivedReport
from app.marts.contracts import (
    MartCandidate,
    QualityContext,
    QualityResult,
    release_version,
)
from app.marts.publication import PublicationOutcome, SnapshotPublisher
from app.marts.quality import QualityGate
from app.marts.source_quality import evaluate_source_quality
from app.content.marts.content import ContentMartBuilder, ContentRepository
from app.macro.marts.macro import MacroMartBuilder, MacroRepository, select_catalog_observations
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.marts.macro_quality import evaluate_macro_quality
from app.market.marts.market import MarketMartBuilder
from app.portfolio.marts.portfolio import PortfolioMartBuilder
from app.stocks.marts.stocks import (
    ScreenerMartBuilder,
    StockWorkbenchMartBuilder,
    load_derived_mart_rows,
    prepare_workbench_inputs,
    project_workbench_cards,
)


def sanitize_error_message(message: str) -> str:
    redacted = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s]+:)[^@\s]+(@)",
        r"\1***\2",
        message,
    )
    redacted = re.sub(
        r"(?i)(\b[A-Z0-9_]*(?:password|passwd|pwd|token|secret|api[_-]?key)[A-Z0-9_]*\b\s*[=:]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^,;\s&]+)",
        r"\1***",
        redacted,
    )
    return re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", redacted)


def _key_part(value: object) -> str:
    if isinstance(value, (date,)):
        return value.isoformat()
    return "" if value is None else str(value)


def _duplicate_source_keys(snapshot: object) -> tuple[str, ...]:
    keys: list[str] = []

    def add_rows(collection: str, rows: object, fields: tuple[str, ...]) -> None:
        for row in rows if isinstance(rows, (list, tuple)) else ():
            parts = tuple(_key_part(getattr(row, field, None)) for field in fields)
            if not parts or not parts[0]:
                continue
            keys.append(
                ":".join((collection, *parts))
            )

    add_rows("core.security", getattr(snapshot, "securities", ()), ("ts_code",))
    add_rows(
        "core.daily_price",
        getattr(snapshot, "daily_prices", ()),
        ("ts_code", "trade_date"),
    )
    add_rows(
        "core.daily_basic",
        getattr(snapshot, "daily_basics", ()),
        ("ts_code", "trade_date"),
    )
    add_rows(
        "core.adjustment_factor",
        getattr(snapshot, "adjustment_factors", ()),
        ("ts_code", "trade_date"),
    )
    for point in getattr(snapshot, "financial_points", ()):
        source_record_key = _key_part(getattr(point, "source_record_key", None))
        if not source_record_key:
            continue
        keys.append(
            ":".join(
                (
                    _key_part(getattr(point, "source_table", None) or "core.financial"),
                    source_record_key,
                )
            )
        )
    action_fields = {
        "Dividend": (
            "ts_code", "end_date", "cash_div", "cash_div_tax",
            "record_date", "ex_date", "pay_date",
        ),
        "Repurchase": ("ts_code", "ann_date", "proc"),
        "ShareUnlock": ("ts_code", "ann_date", "float_date"),
    }
    for action in getattr(snapshot, "corporate_actions", ()):
        name = type(action).__name__
        fields = action_fields.get(name)
        if fields is not None:
            keys.append(
                ":".join(
                    (f"core.{name.lower()}", *(_key_part(getattr(action, field, None)) for field in fields))
                )
            )
    add_rows(
        "core.classification",
        getattr(snapshot, "classifications", ()),
        ("provider", "source", "classification_id"),
    )
    add_rows(
        "core.classification_member",
        getattr(snapshot, "classification_members", ()),
        ("classification_id", "ts_code", "effective_from"),
    )
    add_rows(
        "core.macro_observation",
        getattr(snapshot, "macro_observations", ()),
        ("source", "series_id", "observed_at", "released_at", "revision"),
    )
    add_rows(
        "core.content_document",
        getattr(snapshot, "content_documents", ()),
        ("dedupe_key",),
    )
    return tuple(
        sorted(key for key, count in Counter(keys).items() if count > 1)
    )


@dataclass(frozen=True)
class BuildAndPublishOptions:
    as_of: date
    symbols: tuple[str, ...] = ()
    workbench_symbols: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BuildAndPublishReport:
    status: str
    job_run_id: str
    as_of: date
    version: str | None
    symbols: tuple[str, ...]
    metric_count: int
    risk_count: int
    products: tuple[str, ...]
    quality: tuple[QualityResult, ...]
    missing_data: tuple[dict[str, object], ...]
    error: str | None = None
    release_version: str | None = None
    promoted_products: tuple[str, ...] = ()
    current_versions: tuple[tuple[str, str], ...] = ()


class BuildAndPublishJob:
    def __init__(
        self,
        repository,
        derived,
        builders,
        quality: QualityGate,
        publisher: SnapshotPublisher,
        history,
        sanitize_error=None,
    ) -> None:
        self._repository = repository
        self._derived = derived
        self._builders = builders
        self._quality = quality
        self._publisher = publisher
        self._history = history
        if sanitize_error is None:
            self._sanitize_error = sanitize_error_message
        else:
            self._sanitize_error = lambda message: sanitize_error(
                sanitize_error_message(message)
            )

    def run(self, options: BuildAndPublishOptions) -> BuildAndPublishReport:
        run_id = self._history.start(options)
        derived = DerivedReport(tuple(options.symbols), 0, 0, 0, 0)
        candidates: tuple[MartCandidate, ...] = ()
        results: tuple[QualityResult, ...] = ()
        version: str | None = None
        published: PublicationOutcome | None = None
        try:
            snapshot = self._repository.load(options.as_of, options.symbols)
            derived = self._derived.run(snapshot, options.symbols)
            if options.workbench_symbols is None:
                candidates = tuple(
                    self._builders.build(snapshot, options.symbols, "pending")
                )
            else:
                candidates = tuple(
                    self._builders.build(
                        snapshot,
                        options.symbols,
                        "pending",
                        workbench_symbols=options.workbench_symbols,
                    )
                )
            requested_workbenches = (
                options.workbench_symbols
                if options.workbench_symbols is not None
                else options.symbols
            )
            context = self._quality_context(
                snapshot,
                requested_workbenches,
                derived,
            )
            context = self._baseline_context(context, snapshot)
            results = tuple(self._quality.evaluate(candidates, context))
            candidates = self._merge_advisory_missing_data(candidates, results)
            publication_quality = results
            macro_candidates = tuple(item for item in candidates if item.product in {"macro-transmission", "macro-insights"})
            if macro_candidates:
                catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
                selected = select_catalog_observations(snapshot.macro_observations, catalog, macro_candidates[0].as_of)
                macro_quality = evaluate_macro_quality(catalog, macro_candidates, (), observations=selected,
                    enterprise_analysis=getattr(self._builders, 'enterprise_analysis', None),
                    enterprise_required=getattr(self._builders, 'enterprise_required', False),
                    extension_expected=getattr(self._builders, 'extension_insights', None),
                    extension_required=getattr(self._builders, 'extension_required', False))
                results = (*results, *macro_quality)
                if any(item.severity == "blocking" and not item.passed for item in macro_quality):
                    candidates = tuple(item for item in candidates if item.product not in {"macro-transmission", "macro-insights"})
                else:
                    publication_quality = results
            version = release_version(options.as_of, candidates)
            self._history.record_quality(run_id, results)
            published = self._publisher.publish_many(candidates, version, publication_quality)
            self._history.finish(run_id, "completed")
            return self._report(
                run_id,
                options,
                version,
                derived,
                candidates,
                results,
                published=published,
            )
        except Exception as exc:
            if results == ():
                results = (
                    QualityResult(
                        "build-and-publish",
                        "blocking",
                        False,
                        {"errorType": type(exc).__name__},
                    ),
                )
                self._history.record_quality(run_id, results)
            message = self._sanitize_error(str(exc) or type(exc).__name__) or type(exc).__name__
            try:
                self._history.finish(run_id, "failed", message)
            except Exception:
                pass
            return self._report(
                run_id,
                options,
                version,
                derived,
                candidates,
                results,
                error=message,
                published=published,
            )

    def _baseline_context(self, context: QualityContext, snapshot) -> QualityContext:
        current_reader = getattr(self._publisher, "current", None)
        previous = current_reader("screener") if callable(current_reader) else None
        if previous is None or previous.as_of.date() > snapshot.as_of:
            return context
        rows = previous.payload.get("stocks")
        if not isinstance(rows, list):
            return context
        active = {getattr(row, "ts_code", None) for row in snapshot.securities}
        from app.config import Settings
        policy = Settings.from_env()
        return replace(context,
            baseline_screener_rows=tuple(row for row in rows if isinstance(row, dict) and row.get("symbol") in active),
            max_row_drop_ratio=getattr(policy, "quality_max_row_drop_ratio", 0.2),
            max_field_drop_ratio=getattr(policy, "quality_max_field_drop_ratio", 0.2))

    @staticmethod
    def _quality_context(
        snapshot,
        symbols: tuple[str, ...],
        derived: DerivedReport,
    ) -> QualityContext:
        missing = tuple(
            sorted(
                {
                    str(item["source"])
                    for item in snapshot.missing_data
                    if item.get("source")
                }
            )
        )
        latest_prices = {}
        for row in getattr(snapshot, "daily_prices", ()):
            if row.trade_date <= snapshot.as_of:
                previous = latest_prices.get(row.ts_code)
                if previous is None or row.trade_date > previous.trade_date:
                    latest_prices[row.ts_code] = row
        active_codes = {getattr(row, "ts_code", None) for row in snapshot.securities}
        expected_symbols = tuple(sorted(code for code, row in latest_prices.items()
                                       if code in active_codes and getattr(row, "close", None) is not None))
        return QualityContext(
            eligible_security_count=len(snapshot.securities),
            requested_workbenches=tuple(symbols),
            declared_lineage_count=derived.declared_lineage_count,
            persisted_lineage_count=derived.persisted_lineage_count,
            missing_sources=missing,
            source_duplicate_keys=_duplicate_source_keys(snapshot),
            source_quality_results=(*evaluate_source_quality(snapshot), QualityResult(
                "lineage-inputs-match", "blocking", not derived.lineage_errors,
                {"errors": list(derived.lineage_errors)})),
            expected_screener_symbols=expected_symbols,
        )

    @staticmethod
    def _merge_advisory_missing_data(
        candidates: tuple[MartCandidate, ...],
        results: tuple[QualityResult, ...],
    ) -> tuple[MartCandidate, ...]:
        advisory: list[dict[str, object]] = []
        for result in results:
            if result.severity != "advisory" or result.passed:
                continue
            sources = result.details.get("missingSources")
            if isinstance(sources, (list, tuple)):
                advisory.extend(
                    {"source": str(source), "check": result.name}
                    for source in sources
                )
            else:
                advisory.append({"source": result.name, "check": result.name})

        merged_candidates: list[MartCandidate] = []
        for candidate in candidates:
            records = [*candidate.missing_data, *advisory]
            unique = {
                json.dumps(record, ensure_ascii=False, sort_keys=True, default=str): record
                for record in records
            }
            missing = tuple(unique[key] for key in sorted(unique))
            payload = dict(candidate.payload)
            payload["missingData"] = list(missing)
            merged_candidates.append(
                replace(candidate, payload=payload, missing_data=missing)
            )
        return tuple(merged_candidates)

    @staticmethod
    def _report(
        run_id: str,
        options: BuildAndPublishOptions,
        version: str | None,
        derived: DerivedReport,
        candidates: tuple[MartCandidate, ...],
        results: tuple[QualityResult, ...],
        error: str | None = None,
        published: PublicationOutcome | None = None,
    ) -> BuildAndPublishReport:
        missing: list[dict[str, object]] = []
        for candidate in candidates:
            missing.extend(candidate.missing_data)
        unique_missing = {
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str): record
            for record in missing
        }
        current_versions = tuple(
            sorted(
                (str(row.product), str(row.version))
                for row in (published or ())
            )
        )
        distinct_current_versions = {version for _product, version in current_versions}
        current_version = (
            next(iter(distinct_current_versions))
            if len(distinct_current_versions) == 1
            else None
        )
        promoted_products = published.promoted_products if published is not None else ()
        return BuildAndPublishReport(
            status="failed" if error is not None else "completed",
            job_run_id=run_id,
            as_of=options.as_of,
            version=current_version,
            symbols=tuple(
                options.workbench_symbols
                if options.workbench_symbols is not None
                else options.symbols
            ),
            metric_count=derived.metric_count,
            risk_count=sum(len(group["items"]) for candidate in candidates if candidate.product in promoted_products
                           for group in candidate.payload.get("risks", {}).get("groups", [])),
            products=tuple(candidate.product for candidate in candidates),
            quality=results,
            missing_data=tuple(
                unique_missing[key] for key in sorted(unique_missing)
            ),
            error=error,
            release_version=version,
            promoted_products=promoted_products,
            current_versions=current_versions,
        )


class DefaultMartBuilders:
    def __init__(self, session_factory, *, settings=None) -> None:
        self._session_factory = session_factory
        content = ContentMartBuilder(ContentRepository(session_factory))
        self._market_content = content
        catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        from app.macro.enterprise_contract import enterprise_loader
        from app.macro.extensions.view import load_extension_inputs
        self.enterprise_analysis = None
        self.enterprise_required = settings is not None
        self.extension_insights = None
        self.extension_required = settings is not None
        self._macro = MacroMartBuilder(
            MacroRepository(session_factory), catalog=catalog,
            state_repository=MacroSeriesStateRepository(session_factory, catalog.catalog_version),
            enterprise_loader=enterprise_loader(session_factory, settings) if settings is not None else None,
            extension_loader=(lambda cutoff: load_extension_inputs(session_factory, cutoff)) if settings is not None else None,
        )
        self._screener = ScreenerMartBuilder(session_factory)
        self._workbench = StockWorkbenchMartBuilder(session_factory)
        self._portfolio = PortfolioMartBuilder(session_factory)

    def build(
        self,
        snapshot,
        symbols: tuple[str, ...],
        version: str,
        *,
        workbench_symbols: tuple[str, ...] | None = None,
    ) -> tuple[MartCandidate, ...]:
        from app.market.pulse.repository import validated_statuses
        from app.market.region.repository import valid_regions
        from app.market.data.catalog import MarketDataCatalog
        with self._session_factory() as session:
            checked_at = datetime.now(UTC)
            statuses = validated_statuses(session, snapshot.daily_basics, checked_at)
            regions = valid_regions(session, snapshot.securities, checked_at, MarketDataCatalog.load().region.refresh_interval_days)
            from app.market.etf.repository import market_tiles
            etf_tiles = market_tiles(session, MarketDataCatalog.load(), snapshot.as_of, checked_at)
            from app.market.commodities.publication import market_package
            commodity_package = market_package(session, MarketDataCatalog.load(), snapshot.as_of, checked_at)
        market_overview, market_explorer = MarketMartBuilder(
            self._market_content, limit_statuses=statuses, regions=regions, etf_tiles=etf_tiles, commodity_package=commodity_package, sentiment=MarketDataCatalog.load().sentiment).build(snapshot, version)
        self.enterprise_analysis = self._macro.load_enterprise_analysis(snapshot.as_of)
        self.extension_insights = self._macro.load_extension_insights(snapshot.as_of, snapshot.macro_observations)
        macro_transmission, macro_insights = self._macro.build(
            snapshot.as_of,
            version,
            observations=snapshot.macro_observations,
            enterprise_analysis=self.enterprise_analysis,
            extension_insights=self.extension_insights,
        )
        derived_rows = load_derived_mart_rows(
            self._session_factory,
            (security.ts_code for security in snapshot.securities),
            snapshot.as_of,
        )
        screener = self._screener.build(snapshot, version, derived_rows=derived_rows)
        portfolio = self._portfolio.build(snapshot, version)
        requested_workbenches = (
            workbench_symbols
            if workbench_symbols is not None
            else (symbols or tuple(snapshot.security_inputs))
        )
        workbench_inputs = prepare_workbench_inputs(snapshot,self._session_factory)
        workbenches = tuple(
            self._workbench.build(
                snapshot,
                symbol,
                version,
                derived_rows=derived_rows,
                prepared_inputs=workbench_inputs,
            )
            for symbol in requested_workbenches
            if symbol in snapshot.security_inputs
        )
        return (
            market_overview,
            market_explorer,
            macro_transmission,
            macro_insights,
            screener,
            portfolio,
            *project_workbench_cards(self._session_factory, workbenches),
        )
