from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    PageSnapshot,
    PublishedSnapshot,
    StockDailyChart,
    StockScreenerDaily,
    StockWorkbenchSnapshot,
)
from app.marts.contracts import MartCandidate, QualityResult


class QualityCheckFailed(Exception):
    def __init__(self, failures: Sequence[str]) -> None:
        super().__init__("质量门禁失败: " + ", ".join(failures))
        self.failures = tuple(failures)


DataQualityBlocked = QualityCheckFailed


class PublicationBaseChanged(Exception):
    """构建期间父快照发生变化，事务必须保持原样。"""


@dataclass(frozen=True)
class SnapshotCandidate:
    product: str
    version: str
    as_of: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class QualityCheck:
    name: str
    predicate: Callable[[dict[str, Any]], bool]


def _utc_datetime(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc)


@dataclass(frozen=True)
class PublicationOutcome(Sequence[PublishedSnapshot]):
    snapshots: tuple[PublishedSnapshot, ...]
    promoted_products: tuple[str, ...]

    def __getitem__(self, index: int) -> PublishedSnapshot:
        return self.snapshots[index]

    def __len__(self) -> int:
        return len(self.snapshots)

    def __iter__(self) -> Iterator[PublishedSnapshot]:
        return iter(self.snapshots)


SHARED_HISTORY_PRODUCTS = frozenset(("screener", "portfolio", "market-overview", "macro-insights", "macro-transmission"))


def shared_history_digest(session: Session, candidate: MartCandidate, version: str) -> str | None:
    if candidate.product not in SHARED_HISTORY_PRODUCTS or "marketNews" in candidate.payload or "dailyChart" in candidate.payload:
        return None
    if session.get_bind().dialect.name != "postgresql":
        return None
    # 兼容尚未执行 0021 的底座。迁移用生成列及外键同时保护产品与版本。
    key = "has_referenced_snapshot_history"
    if key not in session.info:
        session.info[key] = session.scalar(text("""
            SELECT EXISTS (SELECT 1 FROM pg_attribute
              WHERE attrelid='mart.published_snapshot'::regclass AND attname='history_version' AND NOT attisdropped)
        """))
    if not session.info[key]:
        return None
    return session.execute(text("""
        SELECT history_generation FROM mart.page_snapshot
        WHERE product=:product AND snapshot_version=:version
    """), {"product": candidate.product, "version": version}).scalar_one()


class SnapshotPublisher:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def publish_many(
        self,
        candidates: Sequence[MartCandidate],
        version: str,
        quality_results: Sequence[QualityResult],
        *,
        expected_versions: dict[str, str] | None = None,
        expected_freshness: dict[str, tuple[bool, str | None]] | None = None,
        preserve_freshness_check: bool = False,
    ) -> PublicationOutcome:
        failures = [
            result.name
            for result in quality_results
            if result.severity == "blocking" and not result.passed
        ]
        if failures:
            raise QualityCheckFailed(failures)
        self._validate_release(candidates, version)
        with self._session_factory.begin() as session:
            if expected_versions is not None:
                parents = tuple(session.scalars(
                    select(PublishedSnapshot).where(PublishedSnapshot.product.in_(expected_versions))
                    .order_by(PublishedSnapshot.product).with_for_update()
                ))
                current = {p.product: p.version for p in parents}
                if current != expected_versions:
                    raise PublicationBaseChanged()
                if expected_freshness is not None and {
                    p.product: (p.stale, p.stale_reason) for p in parents
                } != expected_freshness:
                    raise PublicationBaseChanged()
            from app.stocks.marts.metric_cards import persist_projected_cards
            from app.stocks.marts.risk_cards import persist_current_risks, verify_risk_sources
            persist_projected_cards(session, candidates)
            verify_risk_sources(session, candidates)
            from app.stocks.marts.shareholder_returns import verify_returns
            verify_returns(session, candidates)
            published: list[PublishedSnapshot] = []
            promoted: list[str] = []
            macro_products = {"macro-transmission", "macro-insights"}
            macro_candidates = {item.product: item for item in candidates if item.product in macro_products}
            macro_pointers: dict[str, PublishedSnapshot] = {}
            skip_macro_pair = False
            if set(macro_candidates) == macro_products:
                macro_pointers = {
                    pointer.product: pointer
                    for pointer in session.scalars(
                        select(PublishedSnapshot)
                        .where(PublishedSnapshot.product.in_(sorted(macro_products)))
                        .order_by(PublishedSnapshot.product)
                        .with_for_update()
                    ).all()
                }
                skip_macro_pair = any(
                    _utc_datetime(macro_pointers[product].as_of) > _utc_datetime(candidate.as_of)
                    for product, candidate in macro_candidates.items() if product in macro_pointers
                )
            for candidate in candidates:
                candidate = replace(candidate, as_of=_utc_datetime(candidate.as_of))
                self._write_structured_rows(session, candidate, version)
                self._write_page_snapshot(session, candidate, version)
                if skip_macro_pair and candidate.product in macro_products:
                    if candidate.product in macro_pointers:
                        published.append(macro_pointers[candidate.product])
                    continue
                previous = session.get(PublishedSnapshot, candidate.product) if preserve_freshness_check else None
                checked_at = previous.freshness_checked_at if previous is not None else None
                current, was_promoted = self._switch_pointer(session, candidate, version)
                if current.product.startswith("stock-workbench:") and current.version == version:
                    persist_current_risks(session, current)
                if previous is not None:
                    current.freshness_checked_at = checked_at
                published.append(current)
                if was_promoted:
                    promoted.append(candidate.product)
            session.flush()
            return PublicationOutcome(tuple(published), tuple(promoted))

    @staticmethod
    def _validate_release(
        candidates: Sequence[MartCandidate],
        version: str,
    ) -> None:
        if not version.strip():
            raise ValueError("发布版本不能为空")
        products = [candidate.product for candidate in candidates]
        duplicates = {product for product in products if products.count(product) > 1}
        if duplicates:
            raise ValueError(f"发布产品重复: {', '.join(sorted(duplicates))}")

    def _write_structured_rows(
        self,
        session: Session,
        candidate: MartCandidate,
        version: str,
    ) -> None:
        as_of = candidate.as_of.date()
        screener_rows = tuple(
            (str(row["symbol"]), row)
            for row in candidate.rows.screener
            if isinstance(row.get("symbol"), str)
        )
        existing_screener = {}
        if screener_rows:
            existing_screener = {
                row.ts_code: row
                for row in session.scalars(
                    select(StockScreenerDaily).where(
                        StockScreenerDaily.ts_code.in_(
                            tuple(symbol for symbol, _row in screener_rows)
                        ),
                        StockScreenerDaily.as_of == as_of,
                        StockScreenerDaily.snapshot_version == version,
                    )
                ).all()
            }
        for symbol, row in screener_rows:
            existing = existing_screener.get(symbol)
            if existing is None:
                session.add(
                    StockScreenerDaily(
                        ts_code=symbol,
                        as_of=as_of,
                        snapshot_version=version,
                        fields=dict(row),
                    )
                )
            else:
                existing.fields = dict(row)

        workbench_rows = tuple(
            (str(row["symbol"]), row)
            for row in candidate.rows.workbenches
            if isinstance(row.get("symbol"), str)
        )
        existing_workbenches = {}
        if workbench_rows:
            existing_workbenches = {
                row.ts_code: row
                for row in session.scalars(
                    select(StockWorkbenchSnapshot).where(
                        StockWorkbenchSnapshot.ts_code.in_(
                            tuple(symbol for symbol, _row in workbench_rows)
                        ),
                        StockWorkbenchSnapshot.snapshot_version == version,
                    )
                ).all()
            }
        for symbol, row in workbench_rows:
            from app.stocks.marts.risk_cards import history_payload
            row = history_payload(candidate.product, row)
            existing = existing_workbenches.get(symbol)
            if existing is None:
                session.add(
                    StockWorkbenchSnapshot(
                        ts_code=symbol,
                        as_of=as_of,
                        snapshot_version=version,
                        payload=dict(row),
                    )
                )
            else:
                existing.as_of = as_of
                existing.payload = dict(row)

        symbol = candidate.product.split(":", 1)[1] if ":" in candidate.product else None
        chart_rows: list[tuple[date, dict[str, Any]]] = []
        for row in candidate.rows.charts:
            trade_date = row.get("date") or row.get("tradeDate") or row.get("trade_date")
            if isinstance(trade_date, str):
                trade_date = date.fromisoformat(trade_date[:10])
            if isinstance(trade_date, date) and isinstance(symbol, str):
                chart_rows.append((trade_date, row))
        existing_charts = {}
        if chart_rows and isinstance(symbol, str):
            existing_charts = {
                row.trade_date: row
                for row in session.scalars(
                    select(StockDailyChart).where(
                        StockDailyChart.ts_code == symbol,
                        StockDailyChart.trade_date.in_(
                            tuple(trade_date for trade_date, _row in chart_rows)
                        ),
                        StockDailyChart.snapshot_version == version,
                    )
                ).all()
            }
        for trade_date, row in chart_rows:
            existing = existing_charts.get(trade_date)
            if existing is None:
                session.add(
                    StockDailyChart(
                        ts_code=symbol,
                        trade_date=trade_date,
                        snapshot_version=version,
                        point=dict(row),
                    )
                )
            else:
                existing.point = dict(row)

    def _write_page_snapshot(
        self,
        session: Session,
        candidate: MartCandidate,
        version: str,
    ) -> None:
        from app.stocks.marts.risk_cards import history_payload
        payload = history_payload(candidate.product, candidate.payload)
        existing = session.scalar(
            select(PageSnapshot).where(
                PageSnapshot.product == candidate.product,
                PageSnapshot.snapshot_version == version,
            )
        )
        if existing is None:
            session.add(
                PageSnapshot(
                    product=candidate.product,
                    snapshot_version=version,
                    as_of=candidate.as_of,
                    payload=dict(payload),
                )
            )
        elif history_payload(candidate.product, existing.payload) != payload:
            raise ValueError(
                f"产品 {candidate.product} 的版本 {version} 内容不一致"
            )
        elif _utc_datetime(existing.as_of) != _utc_datetime(candidate.as_of):
            raise ValueError(
                f"产品 {candidate.product} 的版本 {version} as_of 不一致"
            )

    @staticmethod
    def _switch_pointer(
        session: Session,
        candidate: MartCandidate,
        version: str,
    ) -> tuple[PublishedSnapshot, bool]:
        current = session.scalar(
            select(PublishedSnapshot)
            .where(PublishedSnapshot.product == candidate.product)
            .with_for_update()
        )
        published_at = datetime.now(timezone.utc)
        if current is None:
            current = PublishedSnapshot(
                product=candidate.product,
                version=version,
                as_of=candidate.as_of,
                payload=dict(candidate.payload),
                stale=candidate.stale,
                stale_reason=candidate.stale_reason,
                freshness_checked_at=published_at,
                published_at=published_at,
            )
            if digest := shared_history_digest(session, candidate, version):
                current.use_history(digest, dict(candidate.payload))
            session.add(current)
            return current, True
        else:
            current_time = _utc_datetime(current.as_of)
            candidate_time = _utc_datetime(candidate.as_of)
            newer = (
                current_time > candidate_time
                if candidate.product in {"macro-transmission", "macro-insights"}
                else current_time.date() > candidate_time.date()
            )
            if newer:
                return current, False
            if candidate.product.startswith('stock-workbench:'):
                from app.stocks.marts.metric_cards import guard_existing_cards
                guard_existing_cards(current, candidate)
                from app.stocks.marts.shareholder_returns import guard_returns
                guard_returns(current, candidate)
                if (current.payload.get("risks", {}).get("ruleVersion") and not candidate.risk_input_signature
                        and not (candidate.shareholder_return_signature and current.payload.get("risks") == candidate.payload.get("risks"))):
                    raise PublicationBaseChanged("WORKBENCH_RISK_PARENT_CHANGED")
            if candidate.product == 'market-explorer':
                from app.market.marts.region_heatmap import region_coverage_regressed
                if region_coverage_regressed(current.payload,candidate.payload):
                    # 末端共同门禁：普通Builder/股票发布也不能绕过专用入口保护。
                    # 外层事务回滚本批历史、指针和结构化行，避免半对发布。
                    raise QualityCheckFailed(('market.region_coverage',))
                from app.market.etf.coverage import etf_coverage_regressed
                if etf_coverage_regressed(current.payload,candidate.payload):
                    raise QualityCheckFailed(('market.etf_coverage',))
                from app.market.commodities.coverage import commodity_coverage_regressed
                if commodity_coverage_regressed(current.payload,candidate.payload):
                    raise QualityCheckFailed(('market.commodity_coverage',))
            digest = shared_history_digest(session, candidate, version)
            was_promoted = current.version != version
            current.version = version
            current.as_of = candidate.as_of
            if digest:
                current.use_history(digest, dict(candidate.payload))
            else:
                current.payload = dict(candidate.payload)
            current.stale = candidate.stale
            current.stale_reason = candidate.stale_reason
            current.freshness_checked_at = published_at
            current.published_at = published_at
            return current, was_promoted

    def publish(
        self,
        candidate: SnapshotCandidate,
        checks: Sequence[QualityCheck],
    ) -> PublishedSnapshot:
        results = tuple(
            QualityResult(
                check.name,
                "blocking",
                check.predicate(candidate.payload),
                {},
            )
            for check in checks
        )
        mart_candidate = MartCandidate(
            product=candidate.product,
            as_of=candidate.as_of,
            payload=candidate.payload,
        )
        return self.publish_many((mart_candidate,), candidate.version, results)[0]

    def current(self, product: str) -> PublishedSnapshot | None:
        with self._session_factory() as session:
            return session.get(PublishedSnapshot, product)

    def current_macro_pair(self) -> tuple[PublishedSnapshot, ...]:
        with self._session_factory() as session:
            return tuple(session.scalars(select(PublishedSnapshot).where(
                PublishedSnapshot.product.in_(('macro-insights', 'macro-transmission')))).all())
