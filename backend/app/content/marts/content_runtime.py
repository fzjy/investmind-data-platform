"""Explicit database-only publication of content on existing securities marts."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import ContentDocument, PublishedSnapshot
from app.db.session import create_engine_and_session
from app.content.content_policy import DISPLAY_STATUSES
from app.content.marts.content import group_workbench_content
from app.shared.trading_days import load_trading_days
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.content.marts.content_publication import content_candidate, quality_candidate, redact_history
from app.marts.contracts import QualityContext, QualityResult, release_version
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate


def _report(status: str, code: str | None, as_of: datetime | None, **details: Any) -> dict[str, Any]:
    return {"status": status, "code": code, "asOf": as_of.isoformat() if as_of else None,
            "products": [], "promotedProducts": [], "currentVersions": {}, "quality": [],
            "contentCount": 0, "requestCount": 0, "copyrightVerified": False, **details}


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _rebuild(factory: sessionmaker[Session], as_of: datetime) -> dict[str, Any]:
    publisher = SnapshotPublisher(factory)
    with factory.begin() as session:
        bases = session.scalars(select(PublishedSnapshot).where(or_(
            PublishedSnapshot.product == "market-explorer",
            PublishedSnapshot.product.startswith("stock-workbench:"),
        )).order_by(PublishedSnapshot.product).with_for_update()).all()
        documents = session.scalars(select(ContentDocument).where(ContentDocument.published_at <= as_of)
            .order_by(ContentDocument.published_at.desc(), ContentDocument.id.desc()).with_for_update()).all()
        if not documents:
            return _report("no_change", "CONTENT_NO_DOCUMENTS", as_of)
        if not bases:
            redacted = redact_history(session, documents)
            if redacted:
                return _report("completed", "CONTENT_HISTORY_REDACTED", as_of,
                    redactedHistoryCount=redacted, contentCount=len(documents))
            return _report("blocked", "CONTENT_NO_BASE_SNAPSHOT", as_of, contentCount=len(documents))
        visible = [row for row in documents if row.copyright_status in DISPLAY_STATUSES]
        symbols = [base.product.split(":", 1)[1] for base in bases if base.product.startswith("stock-workbench:")]
        prices = {
            base.payload["symbol"]: [date.fromisoformat(c["date"]) for c in base.payload["dailyChart"]["candles"]
                                     if all(c.get(key) is not None for key in ("open", "high", "low", "close"))]
            for base in bases if base.product.startswith("stock-workbench:")
        }
        workbenches = group_workbench_content(session, visible, symbols, load_trading_days(session, prices))
        candidates = tuple(content_candidate(base, visible, workbenches) for base in bases)
        from app.stocks.api.chart_publication import project_overlay_risks, verify_risk_sources, persist_current_risks
        candidates = project_overlay_risks(session, candidates, {base.product: base.version for base in bases})
        checks: list[QualityResult] = []
        context = QualityContext(0, (), 0, 0, ())
        for base, candidate in zip(bases, candidates):
            checks.extend(QualityGate().evaluate((quality_candidate(candidate, as_of),), context))
            checks.append(QualityResult("content-base-real", "blocking",
                candidate.payload.get("isFallbackSample") is not True and _utc(base.as_of) <= as_of,
                {"product": base.product}))
        quality = [asdict(check) for check in checks]
        if any(check.severity == "blocking" and not check.passed for check in checks):
            return _report("blocked", "CONTENT_QUALITY_BLOCKED", as_of, quality=quality, contentCount=len(visible))
        products = tuple(candidate.product for candidate in candidates)
        redacted = redact_history(session, documents)
        # Stable payload plus base identity avoids reusing redacted history versions.
        material = (release_version(datetime(1970, 1, 1, tzinfo=timezone.utc), candidates),
                    tuple((base.product, base.version) for base in bases))
        version = "content-" + hashlib.sha256(json.dumps(material).encode()).hexdigest()[:32]
        promoted = []
        verify_risk_sources(session, [c for base, c in zip(bases, candidates) if base.payload != c.payload])
        for base, candidate in zip(bases, candidates):
            if candidate.payload == base.payload:
                continue
            freshness_checked_at = base.freshness_checked_at
            publisher._write_structured_rows(session, candidate, version)
            publisher._write_page_snapshot(session, candidate, version)
            current, switched = publisher._switch_pointer(session, candidate, version)
            if switched and current.product.startswith("stock-workbench:"):
                persist_current_risks(session, current)
            # Content cannot clear stale prices or change freshness-check evidence.
            base.freshness_checked_at = freshness_checked_at
            promoted.append(candidate.product)
        session.flush()
        return _report("completed", None, as_of, products=list(products), promotedProducts=promoted,
            currentVersions={base.product: base.version for base in bases}, quality=quality,
            contentCount=len(visible), redactedHistoryCount=redacted,
            baseAsOf={base.product: _utc(base.as_of).isoformat() for base in bases})


def rebuild_content_marts(settings: Settings, *, as_of: datetime | None = None, connection_options: dict | None = None) -> dict[str, Any]:
    """Publish atomically, without collecting content or rebuilding price samples.

    `database_url` is the sole required setting; database_read_url is not used.
    `as_of` defaults to real UTC now and limits content visibility only.
    """
    if not settings.database_url:
        return _report("blocked", "CONTENT_DATABASE_REQUIRED", as_of)
    cutoff = _utc(as_of or datetime.now(timezone.utc))
    engine = None
    lock = None
    acquired = False
    key = data_publication_lock_key()
    try:
        engine, factory = create_engine_and_session(settings.database_url, **({'connection_options': connection_options} if connection_options is not None else {}))
        lock = job_lock_for_engine(engine)
        acquired = lock.try_acquire(key)
        if not acquired:
            return _report("blocked", "CONTENT_LOCK_BUSY", cutoff)
        return _rebuild(factory, cutoff)
    except Exception as exc:
        # Database exceptions may contain credentials and raw statements.
        return _report("failed", "CONTENT_REBUILD_FAILED", cutoff, errorType=type(exc).__name__)
    finally:
        if acquired and lock is not None:
            lock.release(key)
        if engine is not None:
            engine.dispose()
