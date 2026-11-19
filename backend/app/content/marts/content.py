from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ContentDocument, JobCursor
from app.content.announcement_history import JOB as ANNOUNCEMENT_JOB
from app.content.content_policy import DISPLAY_STATUSES, TEXT_STATUSES
from app.content.dataset_query import visible_documents
from app.content.marts.report_details import attach_report_details, forecast_period
from app.shared.trading_days import announcement_dates


class ContentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def displayable(self, as_of: datetime, limit: int = 50, ts_code: str | None = None) -> tuple[ContentDocument, ...]:
        with self._session_factory() as session:
            return tuple(session.scalars(visible_documents(as_of, symbol=ts_code).limit(limit)))


class ContentMartBuilder:
    def __init__(self, repository: ContentRepository) -> None:
        self._repository = repository

    def build(self, as_of: datetime, limit: int = 50, ts_code: str | None = None) -> list[dict[str, Any]]:
        return self.from_documents(self._repository.displayable(as_of, limit, ts_code))

    @staticmethod
    def from_documents(rows: tuple[ContentDocument, ...] | list[ContentDocument]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            if row.copyright_status not in DISPLAY_STATUSES:
                continue
            item = {"id": str(row.id), "time": row.published_at.strftime("%H:%M"), "market": "A股" if row.securities else "市场", "title": row.title, "source": row.source, "publishedAt": row.published_at.isoformat(), "sourceUrl": row.source_url, "contentType": row.content_type, "securities": row.securities, "copyrightStatus": row.copyright_status}
            if row.copyright_status in TEXT_STATUSES:
                item["summary"] = row.summary
                item["body"] = row.body
            result.append(item)
        return result


@dataclass
class WorkbenchContent:
    announcements: list[dict] = field(default_factory=list)
    mentions: int = 0
    latest_mention: str = ""
    coverage: dict = field(default_factory=dict)


def announcement_kind(title):
    for kind, words in (
        ("risk", ("风险提示", "立案", "处罚", "监管函", "问询函", "警示函")),
        ("earnings", ("业绩预告", "业绩快报", "业绩说明会")),
        ("capital", ("分红", "利润分配", "回购", "增持", "减持", "发行", "股权激励")),
        ("governance", ("章程", "股东会", "股东大会", "董事会", "监事会", "管理制度")),
        ("major", ("重组", "重大合同", "收购", "重大事项")),
    ):
        if any(word in title for word in words):
            return kind
    return "other"


def group_workbench_content(session, documents, symbols, trading_days) -> dict[str, WorkbenchContent]:
    """输入按披露时间、文档 ID 倒序；同一份公告可关联多只股票。"""
    selected = set(symbols)
    result = defaultdict(WorkbenchContent)
    for row in documents:
        linked = selected.intersection(row.securities or ())
        if not linked or row.copyright_status not in DISPLAY_STATUSES:
            continue
        announcement = None
        if row.content_type == "announcement":
            announcement, = ContentMartBuilder.from_documents([row])
            announcement.update(kind=announcement_kind(row.title), label=row.title)
            if period := forecast_period(row.title):
                announcement["reportPeriod"] = period.isoformat()
        for symbol in linked:
            projection = result[symbol]
            if not projection.mentions:
                projection.latest_mention = row.published_at.isoformat()
            projection.mentions += 1
            if announcement is not None:
                dates = announcement_dates(row.published_at, trading_days.get(symbol))
                projection.announcements.append({**announcement, **dates})
    cursors = session.execute(select(JobCursor.cursor_key, JobCursor.cursor_value["status"].as_string(),
                                     JobCursor.cursor_value["count"].as_integer()).where(JobCursor.job_name == ANNOUNCEMENT_JOB)
                              .order_by(JobCursor.cursor_key))
    for key, status, count in cursors:
        symbol, first, last = key.split(":")
        days = trading_days.get(symbol)
        if symbol not in selected or not days or last < days.first.isoformat() or first > days.last.isoformat():
            continue
        coverage = result[symbol].coverage
        coverage.update(start=days.first.isoformat(), end=days.last.isoformat())
        coverage.setdefault("windows", []).append({"start": first, "end": last, "status": status, "count": count})
    attach_report_details(session, result)
    return dict(result)


def load_workbench_content(session: Session, symbols, as_of: date, trading_days) -> dict[str, WorkbenchContent]:
    if not symbols:
        return {}
    next_day = datetime.combine(as_of + timedelta(days=1), time.min, ZoneInfo("Asia/Shanghai"))
    documents = session.scalars(visible_documents(datetime.now(timezone.utc)).where(
        ContentDocument.published_at < next_day.astimezone(timezone.utc),
        func.json_array_length(ContentDocument.securities) > 0,
    ))
    return group_workbench_content(session, documents, symbols, trading_days)
