"""仅重建当前已发布股票范围的组合；无供应商HTTP，无派生流水线。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, text

from app.db.models import AdjustmentFactor, DailyPrice, PublishedSnapshot, Security
from app.stocks.api.financial import CoreSnapshot
from app.stocks.api.financial import years_before
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.marts.contracts import QualityContext, QualityResult, release_version
from app.portfolio.marts.portfolio import PortfolioMartBuilder
from app.portfolio.normalizer import normalize_portfolio
from app.marts.publication import PublicationBaseChanged, SnapshotPublisher, _utc_datetime
from app.marts.quality import QualityGate


def _positive(value):
    try:
        numeric = Decimal(str(value))
        return numeric.is_finite() and numeric > 0
    except (InvalidOperation, ValueError):
        return False


def rebuild_portfolio(factory, *, dry_run=True):
    engine = factory.kw['bind']
    lock = job_lock_for_engine(engine)
    key = data_publication_lock_key()
    acquired = not dry_run and lock.try_acquire(key)
    report = {'status': 'blocked', 'dryRun': dry_run, 'requestCount': 0,
              'promotedProducts': [], 'checkedAt': datetime.now(timezone.utc).isoformat()}
    if not dry_run and not acquired:
        return {**report, 'code': 'PORTFOLIO_PUBLICATION_LOCK_BUSY'}
    try:
        with factory(autoflush=False) as session:
            if engine.dialect.name == 'postgresql':
                session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            screen = session.get(PublishedSnapshot, 'screener')
            base = session.get(PublishedSnapshot, 'portfolio')
            if screen is None or base is None:
                return {**report, 'code': 'PORTFOLIO_BASE_REQUIRED'}
            symbols = tuple(r.get('symbol') for r in screen.payload.get('stocks', []))
            if not symbols or any(not isinstance(s, str) or not s for s in symbols) or len(set(symbols)) != len(symbols):
                return {**report, 'code': 'PORTFOLIO_SCOPE_INVALID'}
            as_of = _utc_datetime(screen.as_of).date()
            if _utc_datetime(base.as_of).date() > as_of:
                return {**report, 'code': 'PORTFOLIO_DATE_REGRESSION'}
            # 新经济合同必须独立刷新，禁止用新价格配旧经济输入。
            if base.payload.get('economics'):
                return {**report, 'code': 'PORTFOLIO_ECONOMICS_REBUILD_REQUIRED'}
            previous = {p.product: p.version for p in (screen, base)}
            freshness = {p.product: (p.stale, p.stale_reason) for p in (screen, base)}
            old = deepcopy(base.payload)
            securities = tuple(session.scalars(select(Security).where(Security.ts_code.in_(symbols)).order_by(Security.ts_code)))
            if {s.ts_code for s in securities} != set(symbols) or any(s.list_date and s.list_date > as_of for s in securities):
                return {**report, 'code': 'PORTFOLIO_SECURITY_MISMATCH'}
            start = years_before(as_of, 3)  # 与现有CoreSnapshotRepository保持同一历史窗。
            def rows(model):
                return tuple(session.scalars(select(model).where(model.ts_code.in_(symbols), model.trade_date >= start,
                    model.trade_date <= as_of).order_by(model.ts_code, model.trade_date)))
            prices, factors = rows(DailyPrice), rows(AdjustmentFactor)
            price_keys = {(p.ts_code, p.trade_date) for p in prices}
            factor_keys = {(p.ts_code, p.trade_date) for p in factors}
            listing_dates = {s.ts_code: s.list_date for s in securities}
            source_valid = (set(p.ts_code for p in prices) == set(symbols)
                and len(price_keys) == len(prices) and len(factor_keys) == len(factors)
                and all(_positive(p.close) for p in prices)
                and all(not listing_dates[p.ts_code] or p.trade_date >= listing_dates[p.ts_code] for p in prices))
            if not source_valid:
                return {**report, 'code': 'PORTFOLIO_PRICE_OR_FACTOR_INVALID'}
            target_prices = {p.ts_code: p.close for p in prices if p.trade_date == as_of}
            quotes_match = set(target_prices) == set(symbols) and all(
                _positive(r.get('price')) and Decimal(str(r['price'])) == target_prices[r['symbol']]
                for r in screen.payload['stocks'])
            if not quotes_match:
                # 停牌例外需要独立的真实证据合同，不能拿旧报价冒充当日行情。
                return {**report, 'code': 'PORTFOLIO_TARGET_QUOTE_MISSING'}
            retained = {(p.ts_code, p.trade_date.isoformat()) for p in prices}
            expected_history = {(symbol, p['date']) for symbol in symbols
                for p in old.get('prices', {}).get(symbol, [])
                if start.isoformat() <= p['date'] <= as_of.isoformat()}
            if not expected_history <= retained:
                return {**report, 'code': 'PORTFOLIO_HISTORY_REGRESSION'}
            snapshot = CoreSnapshot(as_of=as_of, securities=securities, daily_prices=prices,
                adjustment_factors=factors, daily_basics=(), security_inputs={}, missing_data=(),
                financial_points=(), corporate_actions=(), classifications=(), macro_observations=(), content_documents=())
            candidate = PortfolioMartBuilder(factory).build(snapshot, 'pending')
            payload = {**normalize_portfolio(old), **candidate.payload}
            candidate = replace(candidate, payload=payload, stale=base.stale, stale_reason=base.stale_reason)
        from app.portfolio.marts.portfolio_ledger import validate_dividend_ledger
        ledger = payload.get('dividendLedger')
        if old.get('benchmarks') and not payload.get('benchmarks'):
            return {**report,'code':'PORTFOLIO_REFERENCE_HISTORY_REQUIRED'}
        ledger_errors = validate_dividend_ledger(ledger, symbols, as_of)
        if ledger_errors or ledger.get('sourceStatus') in {'base_changed','invalid_evidence'}:
            return {**report, 'code':'PORTFOLIO_LEDGER_SOURCE_BLOCKED', 'errors':list(ledger_errors)}
        checks = (*QualityGate().evaluate((candidate,), QualityContext(0, (), 0, 0, ())),
            QualityResult('portfolio-scope', 'blocking',
                {a['id'] for a in payload['assets']} == {*symbols, 'CNY'}, {'symbolCount': len(symbols)}))
        if any(c.severity == 'blocking' and not c.passed for c in checks):
            return {**report, 'code': 'PORTFOLIO_QUALITY_BLOCKED', 'failedChecks': [c.name for c in checks if not c.passed]}
        version = release_version(as_of, (candidate,))
        report.update(asOf=as_of.isoformat(), version=version, symbolCount=len(symbols),
            priceCount=len(prices), listingKnown=sum(s.list_date is not None for s in securities),
            previousVersions=previous, products=['portfolio'])
        changed = payload != old or _utc_datetime(base.as_of).date() != as_of
        if dry_run:
            return {**report, 'status': 'planned', 'wouldPublish': changed}
        with factory() as session:
            now = {p.product: p.version for p in session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(previous)))}
        if now != previous:
            return {**report, 'code': 'PORTFOLIO_BASE_CHANGED'}
        if not changed:
            return {**report, 'status': 'no_change'}
        try:
            outcome = SnapshotPublisher(factory).publish_many((candidate,), version, checks,
                expected_versions=previous, expected_freshness=freshness)
        except PublicationBaseChanged:
            return {**report, 'code': 'PORTFOLIO_BASE_CHANGED'}
        return {**report, 'status': 'completed', 'promotedProducts': list(outcome.promoted_products)}
    finally:
        if acquired:
            lock.release(key)
