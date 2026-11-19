from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select

from app.db.models import PublishedSnapshot
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.marts.contracts import MartCandidate, release_version
from app.marts.publication import SnapshotPublisher
from app.market.data.catalog import resolve_market_targets
from app.market.data.quality import clear_resolved_quote_missing, evaluate_market_quality
from app.market.data.repository import MarketQuoteRepository, aware

PRODUCTS = ('market-overview', 'market-explorer')


def publish_market_quotes(factory, catalog, *, checked_at: datetime, targets=None, planned_at=None) -> dict:
    if checked_at.utcoffset() is None:
        raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
    report = {'status': 'blocked', 'promotedProducts': [], 'requestCount': 0}
    lock = job_lock_for_engine(factory.kw['bind'])
    key = data_publication_lock_key()
    if not lock.try_acquire(key):
        return {**report, 'status': 'locked', 'code': 'MARKET_PUBLICATION_LOCK_BUSY'}
    ingest_acquired = lock.try_acquire('sync-market-data')
    if not ingest_acquired:
        lock.release(key)
        return {**report, 'status':'locked', 'code':'MARKET_SYNC_LOCK_BUSY'}
    try:
        with factory.begin() as session:
            bases = {row.product: row for row in session.scalars(select(PublishedSnapshot)
                .where(PublishedSnapshot.product.in_(PRODUCTS)).with_for_update())}
            if set(bases) != set(PRODUCTS):
                return {**report, 'code': 'MARKET_BASE_PAIR_REQUIRED'}
            repo = MarketQuoteRepository(factory)
            targets = resolve_market_targets(catalog, checked_at, targets, planned_at=planned_at)
            indices, etfs = [], []
            for t in targets:
                rows = repo.complete_window(session, t, checked_at)
                if not rows:
                    return {**report, 'code': 'MARKET_QUOTES_INCOMPLETE'}
                row = rows[-1]
                quote = {'code': t.asset.asset_code, 'name': t.asset.name, 'change': float(row.pct_chg),
                    'tradeDate': row.trade_date.isoformat(), 'capturedAt': aware(row.captured_at).isoformat(),
                    'source': f'tushare:{t.asset.api_name}', 'calendarStatus': 'unknown'}
                if t.asset.asset_type == 'indices':
                    quote.update(value=f'{row.close:,.2f}', market=t.asset.market, intraday=None)
                    indices.append(quote)
                else:
                    etfs.append(quote)
            payloads = {product: deepcopy(base.payload) for product, base in bases.items()}
            prior = {q['code']: q for field in ('globalIndices','etfQuotes')
                for q in payloads['market-overview'].get(field, [])}
            for quote in indices + etfs:
                old = prior.get(quote['code'], {})
                if old.get('tradeDate', '') > quote['tradeDate'] or (
                    old.get('tradeDate') == quote['tradeDate'] and old.get('capturedAt')
                    and datetime.fromisoformat(old['capturedAt']) > datetime.fromisoformat(quote['capturedAt'])):
                    return {**report, 'code': 'MARKET_NEWER_PUBLICATION'}
            payloads['market-overview'].update(globalIndices=indices, etfQuotes=etfs)
            candidates = []
            for product in PRODUCTS:
                base = bases[product]
                payload = payloads[product]
                missing = tuple(payload.get('missingData', ()))
                candidates.append(MartCandidate(product, aware(base.as_of), payload,
                    missing_data=missing, stale=base.stale, stale_reason=base.stale_reason))
            candidates = clear_resolved_quote_missing(candidates, catalog=catalog)
            payloads = {candidate.product: candidate.payload for candidate in candidates}
            checks = evaluate_market_quality(candidates, checked_at=checked_at, catalog=catalog)
            report['quality'] = [asdict(check) for check in checks]
            if any(not c.passed and c.severity == 'blocking' for c in checks):
                return {**report, 'code': 'MARKET_QUALITY_BLOCKED'}
            version = release_version(bases['market-overview'].as_of.date(), candidates)
            report['version'] = version
            if all(base.version == version and base.payload == payloads[product] for product, base in bases.items()):
                return {**report, 'status': 'no_change'}
            publisher = SnapshotPublisher(factory)
            for candidate in candidates:
                publisher._write_page_snapshot(session, candidate, version)
                publisher._switch_pointer(session, candidate, version)
            session.flush()
            return {**report, 'status': 'completed', 'promotedProducts': list(PRODUCTS)}
    finally:
        lock.release('sync-market-data')
        lock.release(key)
