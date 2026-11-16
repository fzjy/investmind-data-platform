"""股东回报快照边界与窄范围发布；不访问供应商。"""
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from sqlalchemy import select, text
from app.db.models import PublishedSnapshot
from app.ingestion.repositories import content_hash
from app.marts.contracts import MartCandidate, MartRows, release_version
from app.marts.publication import PublicationBaseChanged
from app.stocks.marts.metric_cards import MetricCardSourceBusy

FIELDS = ("annualDistributions", "concentrationPeriods")

def json_numbers(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: json_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_numbers(v) for v in value]
    return value

def project_returns(session, candidates):
    from app.stocks.shareholder_returns import load_shareholder_returns
    groups = defaultdict(list)
    for c in candidates:
        groups[c.as_of.date()].append(c)
    result = []
    for day, group in groups.items():
        values = load_shareholder_returns(session, [c.payload["symbol"] for c in group], day)
        for c in group:
            parent = session.get(PublishedSnapshot, c.product)
            data = json_numbers(values[c.payload["symbol"]])
            financial = c.payload["financial"]
            payload = c.payload | {"financial": financial | {"shareholders": financial.get("shareholders", {}) | data}}
            result.append(replace(c, payload=payload, rows=replace(c.rows, workbenches=(payload,)),
                shareholder_return_signature=content_hash(data), workbench_parent_version=parent.version if parent else None))
    return tuple(result)

def verify_returns(session, candidates):
    builds = [c for c in candidates if c.shareholder_return_signature]
    if not builds:
        return
    from sqlalchemy.exc import DBAPIError
    try:
        session.execute(text("LOCK TABLE core.shareholder_count, core.daily_basic, core.income_statement, core.dividend, raw.source_payload, raw.ingestion_batch, ops.job_cursor IN SHARE MODE NOWAIT"))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise MetricCardSourceBusy("SHAREHOLDER_SOURCE_BUSY") from exc
        raise
    fresh = {c.product: c for c in project_returns(session, builds)}
    for c in builds:
        parent = session.get(PublishedSnapshot, c.product, with_for_update=True)
        if (parent.version if parent else None) != c.workbench_parent_version or fresh[c.product].shareholder_return_signature != c.shareholder_return_signature:
            raise PublicationBaseChanged("SHAREHOLDER_SOURCE_OR_PARENT_CHANGED")

def guard_returns(current, candidate):
    if current is None or candidate.shareholder_return_signature or not candidate.product.startswith("stock-workbench:"):
        return
    old = current.payload.get("financial", {}).get("shareholders", {})
    new = candidate.payload.get("financial", {}).get("shareholders", {})
    if any(k in old and old[k] != new.get(k) for k in FIELDS):
        raise PublicationBaseChanged("SHAREHOLDER_PARENT_CHANGED")

def rebuild_shareholder_returns(factory, *, dry_run=True, symbols=()):
    from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
    from app.marts.dto import validate_product_payload
    from app.marts.publication import SnapshotPublisher

    lock = job_lock_for_engine(factory.kw["bind"])
    key = data_publication_lock_key()
    report = dict(requestCount=0, dryRun=dry_run, promotedProducts=[])
    if not dry_run and not lock.try_acquire(key):
        return report | dict(status="locked", code="WORKBENCH_CARD_PUBLICATION_LOCK_BUSY")
    try:
        with factory() as session:
            query = select(PublishedSnapshot).where(PublishedSnapshot.product.startswith("stock-workbench:"))
            if symbols:
                query = query.where(PublishedSnapshot.product.in_(["stock-workbench:" + s for s in symbols]))
            bases = list(session.scalars(query.order_by(PublishedSnapshot.product)))
            previous = {p.product: p.version for p in bases}
            freshness = {p.product: (p.stale, p.stale_reason) for p in bases}
            originals = {p.product: p.payload for p in bases}
            candidates = project_returns(session, [MartCandidate(p.product, p.as_of, p.payload,
                MartRows(charts=tuple(p.payload["dailyChart"]["candles"])), stale=p.stale, stale_reason=p.stale_reason)
                for p in bases])
        changed = tuple(c for c in candidates if originals[c.product] != c.payload)
        if any(validate_product_payload(c.product, c.payload) for c in changed):
            raise ValueError("WORKBENCH_CARD_DTO_INVALID")
        report.update(symbolCount=len(bases), changedCount=len(changed), wouldPublish=bool(changed),
                      changedSymbols=[c.payload["symbol"] for c in changed])
        if dry_run or not changed:
            return report | dict(status="planned" if dry_run else "no_change")
        version = release_version(max(c.as_of.date() for c in changed), changed)
        outcome = SnapshotPublisher(factory).publish_many(changed, version, (), expected_versions=previous,
            expected_freshness=freshness, preserve_freshness_check=True)
        return report | dict(status="completed", version=version, promotedProducts=list(outcome.promoted_products))
    except MetricCardSourceBusy:
        return report | dict(status="locked", code="WORKBENCH_CARD_SOURCE_BUSY_RETRY", retryable=True)
    finally:
        if not dry_run:
            lock.release(key)
