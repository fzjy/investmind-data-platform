"""按证券重算工作台八卡，并在提升指针的同一事务中核对和保存来源。"""
from collections import defaultdict
from dataclasses import replace

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import PublishedSnapshot
from app.ingestion.repositories import content_hash
from app.marts.contracts import MartCandidate, MartRows, release_version
from app.marts.publication import PublicationBaseChanged
from app.stocks.derived.inputs import load_metric_card_inputs
from app.stocks.derived.pipeline import DerivedPipeline
from app.stocks.derived.workbench_cards import (
    CARD_IDS, CARD_VERSION, calculate_metric_cards, display_card, metric_value_card,
)


def _signature(inputs):
    return content_hash([CARD_VERSION, inputs])


def _cards(payload):
    return [card for card in payload.get("metrics", {}).get("cards", []) if card.get("id") in CARD_IDS]


def project_cards(session, candidates, *, shareholder_returns=True):
    groups = defaultdict(list)
    for candidate in candidates:
        groups[candidate.as_of.date()].append(candidate)
    projected = []
    for day, group in groups.items():
        inputs = load_metric_card_inputs(session, (c.payload["symbol"] for c in group), day)
        parents = {p.product: p for p in session.scalars(select(PublishedSnapshot).where(
            PublishedSnapshot.product.in_([c.product for c in group])))}
        for candidate in group:
            symbol = candidate.payload["symbol"]
            if candidate.product != "stock-workbench:" + symbol:
                raise ValueError("WORKBENCH_CARD_SCOPE_CHANGED")
            signature = _signature(inputs[symbol])
            parent = parents.get(candidate.product)
            existing = _cards(parent.payload) if parent else []
            if (parent and parent.payload.get("metrics", {}).get("cardInputSignature") == signature
                    and [c["id"] for c in existing] == list(CARD_IDS)):
                cards = existing
            else:
                cards = [display_card(c.metric_id, day.year, c.value, c.calculation())
                         for c in calculate_metric_cards(inputs[symbol])]
            metrics = candidate.payload["metrics"]
            labels = {c["label"] for c in cards}
            others = [c for c in metrics["cards"] if c.get("id") not in CARD_IDS and c["label"] not in labels]
            payload = candidate.payload | {"metrics": metrics | {"cards": [*cards, *others], "cardInputSignature": signature}}
            projected.append(replace(candidate, payload=payload, rows=replace(candidate.rows, workbenches=(payload,)),
                                     metric_card_signature=signature, workbench_parent_version=parent.version if parent else None))
    from app.stocks.marts.risk_cards import project_risks
    from app.stocks.marts.shareholder_returns import project_returns
    risks = project_risks(session, projected)
    return project_returns(session, risks) if shareholder_returns else risks


class MetricCardSourceBusy(PublicationBaseChanged):
    """采集事务持有输入表写锁，发布可在其提交后重试。"""


def persist_projected_cards(session, candidates):
    builds = [c for c in candidates if c.metric_card_signature]
    if not builds:
        return
    # PostgreSQL 默认 READ COMMITTED。冻结这些来源直到指针提交，亦覆盖新增行/首次覆盖记录。
    if session.get_bind().dialect.name == "postgresql":
        try:
            session.execute(text("LOCK TABLE core.daily_price, core.daily_basic, core.income_statement, "
                                 "core.balance_sheet, core.financial_indicator, core.dividend, raw.ingestion_batch, "
                                 "raw.source_payload, ops.job_cursor IN SHARE MODE NOWAIT"))
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise MetricCardSourceBusy("WORKBENCH_CARD_SOURCE_BUSY_RETRY") from exc
            raise
    groups = defaultdict(list)
    for c in builds:
        groups[c.as_of.date()].append(c)
    for day, group in groups.items():
        inputs = load_metric_card_inputs(session, (c.payload["symbol"] for c in group), day)
        for c in sorted(group, key=lambda row: row.product):
            symbol = c.payload["symbol"]
            parent = session.get(PublishedSnapshot, c.product, with_for_update=True)
            if (parent.version if parent else None) != c.workbench_parent_version:
                raise PublicationBaseChanged("WORKBENCH_CARD_PARENT_CHANGED")
            if _signature(inputs[symbol]) != c.metric_card_signature:
                raise PublicationBaseChanged("WORKBENCH_CARD_INPUT_CHANGED")
            if (parent and parent.payload.get("metrics", {}).get("cardInputSignature") == c.metric_card_signature
                    and _cards(parent.payload) == _cards(c.payload)):
                continue
            rows = DerivedPipeline.run_metric_cards(session, {symbol: inputs[symbol]})[symbol]
            if [metric_value_card(row) for row in rows] != _cards(c.payload):
                raise ValueError("WORKBENCH_CARD_PROJECTION_MISMATCH")


def guard_existing_cards(current, candidate):
    if current is None or candidate.metric_card_signature or not candidate.product.startswith("stock-workbench:"):
        return
    signature = current.payload.get("metrics", {}).get("cardInputSignature")
    if signature and (signature != candidate.payload.get("metrics", {}).get("cardInputSignature")
                      or _cards(current.payload) != _cards(candidate.payload)):
        raise PublicationBaseChanged("WORKBENCH_CARD_PARENT_CHANGED")


def rebuild_metric_cards(factory, *, dry_run=True, symbols=(), shareholder_returns=False):
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
            candidates = project_cards(session, [MartCandidate(p.product, p.as_of, p.payload,
                MartRows(charts=tuple(p.payload["dailyChart"]["candles"])), stale=p.stale, stale_reason=p.stale_reason)
                for p in bases], shareholder_returns=shareholder_returns)
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
