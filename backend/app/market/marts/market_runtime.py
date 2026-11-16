"""仅用已入库的有限股票数据，预检或原子重建两个市场快照。"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.db.models import Classification, ClassificationMember, DailyBasic, DailyPrice, PublishedSnapshot, Security, TradeCalendar
from app.stocks.api.financial import CoreSnapshot
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.marts.contracts import QualityContext, QualityResult, release_version
from app.market.marts.market import MarketMartBuilder
from app.market.commodities.missing import reconcile_commodity_missing
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.shared.stock_mapping import StockMappingPolicy, classification_source, finite_decimal

MARKET_PRODUCTS = ("market-overview", "market-explorer")


def rebuild_market_marts(factory, selection, *, checked_at: datetime, dry_run: bool = True,
                         timezone_name: str = "Asia/Shanghai", sync_hour: int = 19, sync_minute: int = 0,
                         max_classification_drop_ratio: float = 0.2, expected_as_of: date | None = None) -> dict:
    """不调用供应商，不运行派生/证券/宏观构建；共享发布锁保护成对提交。"""
    local = checked_at.astimezone(ZoneInfo(timezone_name))
    engine = factory.kw["bind"]
    lock = job_lock_for_engine(engine)
    key = data_publication_lock_key()
    report = {"status": "blocked", "code": None, "requestCount": 0, "dryRun": dry_run,
              "checkedAt": checked_at.isoformat(), "universe": selection.to_payload(),
              "products": list(MARKET_PRODUCTS), "promotedProducts": []}
    acquired = not dry_run and lock.try_acquire(key)
    if not dry_run and not acquired:
        return {**report, "code": "MARKET_LOCK_BUSY"}
    try:
        with factory.begin() as session:
            if dry_run:
                session.execute(text("SET TRANSACTION READ ONLY"))
            statement = select(PublishedSnapshot).where(PublishedSnapshot.product.in_(MARKET_PRODUCTS))
            bases = {row.product: row for row in session.scalars(statement if dry_run else statement.with_for_update())}
            if set(bases) != set(MARKET_PRODUCTS):
                return {**report, "code": "MARKET_BASE_PAIR_REQUIRED"}
            symbols = selection.symbols
            latest = session.scalar(select(func.max(DailyPrice.trade_date)).where(
                DailyPrice.ts_code.in_(symbols), DailyPrice.trade_date <= local.date()))
            if latest is None:
                return {**report, "code": "MARKET_NO_PRICES"}
            if expected_as_of is not None:
                stock_base = session.get(PublishedSnapshot, 'screener')
                if latest != expected_as_of or stock_base is None or stock_base.as_of.date() != expected_as_of:
                    return {**report, 'code': 'MARKET_TARGET_CHANGED'}
            if any(row.as_of.date() > latest for row in bases.values()):
                return {**report, "code": "MARKET_NEWER_PUBLICATION"}
            securities = tuple(session.scalars(select(Security).where(Security.ts_code.in_(symbols)).order_by(Security.ts_code)))
            prices = tuple(session.scalars(select(DailyPrice).where(DailyPrice.ts_code.in_(symbols), DailyPrice.trade_date == latest).order_by(DailyPrice.ts_code)))
            basics = tuple(session.scalars(select(DailyBasic).where(DailyBasic.ts_code.in_(symbols), DailyBasic.trade_date == latest).order_by(DailyBasic.ts_code)))
            members = tuple(session.scalars(select(ClassificationMember).where(
                ClassificationMember.ts_code.in_(symbols), ClassificationMember.effective_from <= latest,
                (ClassificationMember.effective_to.is_(None) | (ClassificationMember.effective_to >= latest)))))
            policy = StockMappingPolicy.load().classification
            classifications = tuple(row for row in session.scalars(select(Classification).order_by(Classification.id))
                                    if row.provider == policy.provider and classification_source(row) == policy.source)
            classification_ids = {row.id for row in classifications}
            members = tuple(row for row in members if row.classification_id in classification_ids)
            coverage = {"expected": len(symbols), "securities": len(securities), "prices": len(prices),
                        "basics": len(basics), "classified": len({row.ts_code for row in members})}
            report.update(asOf=latest.isoformat(), coverage=coverage,
                          previousVersions={key: row.version for key, row in bases.items()})
            if len(prices) != len(symbols) or len(securities) != len(symbols):
                return {**report, "code": "MARKET_SCOPE_INCOMPLETE"}
            if any(finite_decimal(row.close) is None or finite_decimal(row.close) <= 0 for row in prices) or not any(finite_decimal(row.pct_chg) is not None for row in prices):
                return {**report, "code": "MARKET_VALUES_UNAVAILABLE"}
            snapshot = CoreSnapshot(latest, securities, prices, basics, (), {}, (), (), (), classifications, (), (), members)
            cutoff = local.date()
            calendar_filter = TradeCalendar.cal_date <= cutoff if local.time() >= time(sync_hour, sync_minute) else TradeCalendar.cal_date < cutoff
            expected = session.scalar(select(func.max(TradeCalendar.cal_date)).where(
                TradeCalendar.exchange == "SSE", TradeCalendar.is_open.is_(True), calendar_filter))
            calendar_end = session.scalar(select(func.max(TradeCalendar.cal_date)).where(TradeCalendar.exchange == "SSE"))
            stale_reason = "MARKET_CALENDAR_UNKNOWN" if expected is None or calendar_end is None or calendar_end < local.date() else (
                "SNAPSHOT_BEHIND_EXPECTED_DATE" if latest < expected else None)
            candidates = []
            from app.market.pulse.repository import validated_statuses
            from app.market.region.repository import valid_regions
            from app.market.data.catalog import MarketDataCatalog
            catalog = MarketDataCatalog.load()
            statuses = validated_statuses(session, basics, checked_at)
            regions = valid_regions(session, securities, checked_at, catalog.region.refresh_interval_days)
            from app.market.etf.repository import market_tiles
            etf_tiles = market_tiles(session, catalog, latest, checked_at, bases['market-explorer'].payload)
            from app.market.commodities.publication import market_package
            commodity_package = market_package(session, catalog, latest, checked_at, bases['market-explorer'].payload)
            for candidate in MarketMartBuilder(limit_statuses=statuses, regions=regions, etf_tiles=etf_tiles, commodity_package=commodity_package, sentiment=catalog.sentiment).build(snapshot, "pending"):
                base = bases[candidate.product]
                payload = {**base.payload, **candidate.payload}
                # 本轮只拥有状态补齐来源，不能用通用占位说明覆盖旧的具体故障。
                missing = [row for row in base.payload.get('missingData', [])
                    if row.get('source') not in {'limit_status', 'market:pulse:limit_status'}]
                sources = {row.get('source') for row in missing}
                for row in candidate.missing_data:
                    if row.get('source') not in sources:
                        missing.append(row)
                        sources.add(row.get('source'))
                missing = list(reconcile_commodity_missing(missing, commodity_package))
                payload['missingData'] = missing
                if candidate.product == "market-overview":
                    for field in ("globalIndices", "marketStatuses", "etfQuotes", "fundFlowSnapshot"):
                        if field in base.payload:
                            payload[field] = base.payload[field]
                    payload["marketSnapshot"] = {**payload["marketSnapshot"], "support": f"固定{len(symbols)}股覆盖样本，非全市场；有效行情{len(prices)}/{len(symbols)}",
                                                 "coverage": coverage}
                else:
                    payload["scopeLabel"] = f"固定{len(symbols)}股覆盖样本，非全市场"
                    payload["aggregationLabel"] = "日线收盘聚合"
                    for field in ("marketNews", "commodityCategories", "commodityData", "commodityPeriods"):
                        if field in base.payload and (field == 'marketNews' or ('commodityMetadata' not in base.payload and not commodity_package)):
                            payload[field] = base.payload[field]
                    if 'commodityMetadata' in base.payload and not commodity_package:
                        payload.pop('commodityMetadata', None)
                    other_categories = [row for row in base.payload.get("heatmapCategories", []) if row["id"] not in {"majorIndustry", "subIndustry", "region", "etf"}]
                    payload["heatmapCategories"] = other_categories + candidate.payload["heatmapCategories"]
                    other_data = {key: value for key, value in base.payload.get("heatmapData", {}).items() if key not in {"majorIndustry", "subIndustry", "region", "etf"}}
                    # 旧未标单位的保留分类不能假称已是亿元，须先按旧合同标准化。
                    if base.payload.get("amountUnit") != "yi-yuan" and any(other_data.values()):
                        return {**report, "code": "MARKET_LEGACY_EXTERNAL_HEATMAP"}
                    payload["heatmapData"] = {**other_data, **candidate.payload["heatmapData"]}
                    for category in ("majorIndustry", "subIndustry", "region"):
                        def covered_symbols(data):
                            return {stock["symbol"] for sector in data.get(category, [])
                                if category != 'region' or sector.get('id') != 'region:unknown'
                                for stock in sector.get("stocks", [])}
                        previous_symbols = covered_symbols(base.payload.get("heatmapData", {}))
                        next_symbols = covered_symbols(payload["heatmapData"])
                        if previous_symbols and len(next_symbols & previous_symbols) / len(previous_symbols) < 1 - max_classification_drop_ratio:
                            return {**report, "code": "MARKET_CLASSIFICATION_REGRESSION", "regressedCategory": category}
                reason = base.stale_reason if base.stale and base.stale_reason not in {None, "SNAPSHOT_BEHIND_EXPECTED_DATE", "MARKET_CALENDAR_UNKNOWN"} else stale_reason
                candidates.append(replace(candidate, payload=payload, missing_data=tuple(missing), stale=bool(reason), stale_reason=reason))
            # 报价/新闻时点与股票日分开复核，仍保留原分类退化和故障保护。
            from app.market.data.catalog import MarketDataCatalog
            from app.market.data.quality import clear_resolved_quote_missing, evaluate_market_quality
            catalog = MarketDataCatalog.load()
            candidates = clear_resolved_quote_missing(candidates, catalog=catalog)
            checks = evaluate_market_quality(candidates, checked_at=checked_at, catalog=catalog)
            report.update(quality=[asdict(check) for check in checks], expectedBusinessDate=str(expected) if expected else None,
                          stale=any(item.stale for item in candidates),
                          heatmapCategories=[row["id"] for row in candidates[1].payload["heatmapCategories"]])
            if any(check.severity == "blocking" and not check.passed for check in checks):
                return {**report, "code": "MARKET_QUALITY_BLOCKED"}
            version = release_version(latest, candidates)
            report["version"] = version
            unchanged = all(bases[item.product].payload == item.payload and bases[item.product].as_of.date() == latest
                            and bases[item.product].version == version and bases[item.product].stale == item.stale
                            and bases[item.product].stale_reason == item.stale_reason for item in candidates)
            if dry_run:
                return {**report, "status": "planned", "wouldPublish": not unchanged}
            if unchanged:
                return {**report, "status": "no_change"}
            publisher = SnapshotPublisher(factory)
            for candidate in candidates:
                publisher._write_page_snapshot(session, candidate, version)
                publisher._switch_pointer(session, candidate, version)
            session.flush()
            return {**report, "status": "completed", "promotedProducts": list(MARKET_PRODUCTS)}
    finally:
        if acquired:
            lock.release(key)
