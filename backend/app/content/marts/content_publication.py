"""Content overlays and conservative redaction of readable version history."""
from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import ContentDocument, PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot
from app.content.content_policy import DISPLAY_STATUSES, TEXT_STATUSES
from app.content.marts.content import ContentMartBuilder, WorkbenchContent
from app.marts.contracts import MartCandidate, MartRows

CONTENT_METADATA_KEYS = frozenset((
    "id", "time", "market", "title", "source", "publishedAt", "sourceUrl",
    "contentType", "securities", "copyrightStatus", "date", "kind", "label",
    "disclosedOn", "disclosureTime", "placementStatus", "placementReason",
    "reportPeriod", "financialReport",
))


def content_candidate(base: PublishedSnapshot, documents: list[ContentDocument], workbenches: dict[str, WorkbenchContent]) -> MartCandidate:
    payload = deepcopy(base.payload)
    if base.product == "market-explorer":
        payload["marketNews"] = ContentMartBuilder.from_documents(documents[:50])
        rows = MartRows()
    else:
        symbol = base.product.split(":", 1)[1]
        content = workbenches.get(symbol, WorkbenchContent())
        payload["dailyChart"]["announcements"] = content.announcements
        if content.coverage:
            payload["dailyChart"]["announcementCoverage"] = content.coverage
        else:
            payload["dailyChart"].pop("announcementCoverage", None)
        payload["quote"]["mentions"] = content.mentions
        payload["quote"]["latestMention"] = content.latest_mention
        rows = MartRows(workbenches=(payload,), charts=tuple(payload["dailyChart"]["candles"]))
    return MartCandidate(base.product, base.as_of, payload, rows=rows,
                         stale=base.stale, stale_reason=base.stale_reason)


def redact_history(session: Session, documents: list[ContentDocument]) -> int:
    """Old versions stay readable, so revoked text must be removed there too.

    Unidentifiable legacy entries are removed rather than treated as licensed.
    Only content-owned fields change; history dates/versions stay intact.
    """
    by_url: dict[tuple[str, str], list[ContentDocument]] = defaultdict(list)
    by_id = {(row.source, str(row.id)): row for row in documents}
    for document in documents:
        by_url[(document.source, document.source_url)].append(document)

    def safe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        safe = []
        for item in items:
            matches = by_url.get((item.get("source"), item.get("sourceUrl")), [])
            document = matches[0] if len(matches) == 1 else by_id.get((item.get("source"), str(item.get("id"))))
            if document is not None and document.title != item.get("title"):
                document = None
            if document is None or document.copyright_status not in DISPLAY_STATUSES:
                continue
            sanitized = dict(item)
            sanitized["copyrightStatus"] = document.copyright_status
            if document.copyright_status not in TEXT_STATUSES:
                sanitized = {key: value for key, value in sanitized.items() if key in CONTENT_METADATA_KEYS}
            safe.append(sanitized)
        return safe

    changed = 0
    # 版本读取和工作台回退不要求当前指针存在，必须覆盖全部可读内容历史。
    history = list(session.scalars(select(PageSnapshot).where(or_(
        PageSnapshot.product == "market-explorer",
        PageSnapshot.product.startswith("stock-workbench:"),
    )).order_by(PageSnapshot.id).with_for_update()))
    history.extend(session.scalars(select(StockWorkbenchSnapshot)
        .order_by(StockWorkbenchSnapshot.id).with_for_update()))
    for row in history:
        payload = deepcopy(row.payload)
        if "marketNews" in payload:
            payload["marketNews"] = safe_items(payload["marketNews"])
        if isinstance(payload.get("dailyChart"), dict):
            payload["dailyChart"]["announcements"] = safe_items(payload["dailyChart"].get("announcements", []))
        if payload != row.payload:
            row.payload = payload
            changed += 1
    return changed


def quality_candidate(candidate: MartCandidate, as_of: datetime) -> MartCandidate:
    # 公告可见性已按实际时刻过滤。工作台标点按上海自然日参加日期校验。
    if candidate.product.startswith("stock-workbench:"):
        as_of = as_of.astimezone(ZoneInfo("Asia/Shanghai"))
    return replace(candidate, as_of=as_of)
