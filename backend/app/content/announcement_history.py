"""普通公告的历史窗口与分页检查点。适配器只传已经验证过的公告元数据。"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.connectors.external_files.contracts import ContentImportRecord
from app.db.models import ContentDocument, IngestionBatch, JobCursor, PublishedSnapshot, SourcePayload
from app.ingestion.repositories import content_hash
from app.jobs.locks import job_lock_for_engine
from app.shared.trading_days import candle_dates, chart_history_bounds

JOB = "workbench-cninfo-history-v1"
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Window:
    symbol: str
    start: date
    end: date

    @property
    def key(self):
        return f"{self.symbol}:{self.start}:{self.end}"


@dataclass(frozen=True)
class AnnouncementPage:
    records: tuple[ContentImportRecord, ...]
    total: int  # 该窗口中不同公告 ID 的总数
    next_page: str | None
    response: dict | None = None


def _ranges(start, end, excluded):
    day = start
    while day <= end:
        if day in excluded:
            day += DAY
            continue
        first = day
        while day < end and (day + DAY).month == first.month and day + DAY not in excluded:
            day += DAY
        yield first, day
        day += DAY


def history_plan(session, symbols, *, window_limit=None, revisit_days=0):
    symbols = set(symbols)
    products = {"stock-workbench:" + symbol: symbol for symbol in symbols}
    query = select(PublishedSnapshot.product, PublishedSnapshot.payload["dailyChart"]["candles"]).where(
        PublishedSnapshot.product.in_(products))
    bounds = {}
    for product, candles in session.execute(query):
        days = candle_dates(candles)
        if span := chart_history_bounds(days):
            if window_limit:
                span = max(span[0], window_limit.start), min(span[1], window_limit.end)
                if span[0] > span[1]:
                    continue
            bounds[products[product]] = span
    states = defaultdict(list)
    state_query = select(JobCursor.cursor_key, JobCursor.cursor_value["status"].as_string(),
                         JobCursor.cursor_value["count"].as_integer(),
                         JobCursor.cursor_value["checkedThrough"].as_string(),
                         JobCursor.cursor_value["checkedOn"].as_string()).where(JobCursor.job_name == JOB)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    for key, status, count, checked_through, checked_on in session.execute(state_query):
        symbol, first, last = key.split(":")
        if symbol in bounds:
            states[symbol].append((Window(symbol, date.fromisoformat(first), date.fromisoformat(last)), status, count, checked_through, checked_on))
    pending = []
    summary = {}
    last_review = {}
    for symbol, (start, end) in sorted(bounds.items()):
        completed, reserved = set(), set()
        empty_windows = 0
        for window, status, count, checked_through, checked_on in states[symbol]:
            first, last = max(start, window.start), min(end, window.end)
            if last < first:
                continue
            if status == "completed":
                completed.update(first + DAY * i for i in range((last - first).days + 1))
                if revisit_days and last > end - DAY * revisit_days and (checked_through != end.isoformat() or checked_on != today):
                    review = Window(symbol, first, last)
                    pending.append((review, "recheck"))
                    last_review[review] = checked_on or ""
                if count == 0:
                    empty_windows += 1
            elif (first, last) == (window.start, window.end):
                pending.append((window, status))
                reserved.update(first + DAY * i for i in range((last - first).days + 1))
        pending.extend((Window(symbol, a, b), "unchecked") for a, b in _ranges(start, end, completed | reserved))
        summary[symbol] = {"from": start.isoformat(), "to": end.isoformat(), "checkedDays": len(completed),
                           "pendingDays": (end - start).days + 1 - len(completed), "checkedEmptyWindows": empty_windows}
    pending.sort(key=lambda pair: (-pair[0].end.toordinal(), pair[1] == "recheck",
                                   last_review.get(pair[0], ""), pair[0].symbol))
    return pending, summary, sorted(symbols - bounds.keys())


def _accept_page(window, state, page):
    if type(page.total) is not int or page.total < 0 or state.get("total", page.total) != page.total:
        raise ValueError("公告窗口总数不一致")
    records = dict(state.get("records", {}))
    previous_count = len(records)
    for row in page.records:
        day = row.published_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        host = urlsplit(str(row.source_url))
        if (row.source != "cninfo" or row.content_type != "announcement" or not row.external_id
                or len("cninfo:" + row.external_id) > 128 or row.copyright_status != "link_only"
                or row.summary or row.body or window.symbol not in row.securities or not window.start <= day <= window.end
                or host.scheme != "https" or not (host.hostname == "cninfo.com.cn" or (host.hostname or "").endswith(".cninfo.com.cn"))):
            raise ValueError("公告身份、范围或原文链接未通过校验")
        record = row.model_dump(mode="json")
        if row.external_id in records and records[row.external_id] != record:
            raise ValueError("同一公告在翻页中发生变动")
        records[row.external_id] = record
    pages = list(state.get("pages", []))
    if page.next_page is not None:
        if not page.next_page or page.next_page in pages or len(records) == previous_count:
            raise ValueError("公告下一页游标无效")
        pages.append(page.next_page)
    if len(records) > page.total or (page.next_page is None and len(records) != page.total):
        raise ValueError("公告去重数与来源总数不符")
    return {"status": "partial", "records": records, "pages": pages, "next": page.next_page, "total": page.total}


def preview_pages(window, fetch_page, page_budget):
    """在内存中复用回填校验，不写业务记录或推进历史覆盖。"""
    if page_budget < 1:
        raise ValueError("至少读取一页")
    state = {}
    for page_number in range(1, page_budget + 1):
        page = fetch_page(window, state.get("next"))
        state = _accept_page(window, state, page)
        if state["next"] is None:
            break
    return {
        "symbol": window.symbol, "from": window.start.isoformat(), "to": window.end.isoformat(),
        "pages": page_number, "complete": state["next"] is None, "sourceTotal": state["total"],
        "records": [{"id": record["external_id"], "title": record["title"],
                     "sourceUrl": record["source_url"],
                     "disclosureDate": datetime.fromisoformat(record["published_at"]).astimezone(
                         ZoneInfo("Asia/Shanghai")).date().isoformat()}
                    for record in state["records"].values()],
    }


def _finish(session, records, symbols):
    for item in records.values():
        row = ContentImportRecord.model_validate(item)
        key = f"cninfo:{row.external_id}"
        existing = session.scalar(select(ContentDocument).where(ContentDocument.dedupe_key == key).with_for_update())
        if existing is None:
            existing = ContentDocument(source="cninfo", external_id=row.external_id, dedupe_key=key,
                                       content_type="announcement", copyright_status="link_only")
            session.add(existing)
        existing.title = row.title
        existing.source_url = str(row.source_url)
        existing.published_at = row.published_at
        existing.securities = sorted(set(existing.securities or ()) | symbols.intersection(row.securities))


def backfill_pages(factory, symbols, fetch_page, page_budget, *, window_limit=None, revisit_days=0):
    """一次 fetch_page 对应一页。仅在获权的来源适配器就绪后由调用方注入。"""
    if type(page_budget) is not int or page_budget < 0:
        raise ValueError("页数预算无效")
    symbols = set(symbols)
    lock = job_lock_for_engine(factory.kw["bind"])
    if not lock.try_acquire(JOB):
        return {"status": "locked", "pages": 0}
    used = 0
    try:
        with factory() as session:
            pending, coverage, waiting = history_plan(session, symbols, window_limit=window_limit, revisit_days=revisit_days)
        for window, operation in pending:
            while used < page_budget:
                with factory.begin() as session:
                    cursor = session.scalar(select(JobCursor).filter_by(job_name=JOB, cursor_key=window.key))
                    if cursor is None:
                        cursor = JobCursor(job_name=JOB, cursor_key=window.key, cursor_value={})
                        session.add(cursor)
                    state = {} if operation == "recheck" and cursor.cursor_value.get("status") == "completed" else cursor.cursor_value
                    used += 1
                    try:
                        page = fetch_page(window, state.get("next"))
                    except Exception as exc:
                        cursor.cursor_value = {**state, "status": "failed", "error": type(exc).__name__}
                        return {"status": "failed", "window": window.key, "pages": used}
                    if page.response is not None:
                        batch = IngestionBatch(source="cninfo", dataset=JOB, mode="standard", status="completed",
                                               row_count=len(page.records), request_count=1, finished_at=datetime.now(UTC))
                        session.add(batch)
                        session.flush()
                        session.add(SourcePayload(batch_id=batch.id, api_name="hisAnnouncement/query",
                            request_hash=content_hash([window.key, state.get("next")]), content_hash=content_hash(page.response),
                            payload={"window": window.key, "page": state.get("next") or "1", "response": page.response}))
                    try:
                        state = _accept_page(window, state, page)
                    except ValueError as exc:
                        # 分页结果已不自洽，下次从该窗口首页重新核对。
                        cursor.cursor_value = {"status": "failed", "error": str(exc)}
                        return {"status": "failed", "window": window.key, "pages": used}
                    if state["next"] is None:
                        _finish(session, state["records"], symbols)
                        cursor.cursor_value = {"status": "completed", "count": len(state["records"]),
                                               "checkedOn": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                                               "checkedThrough": coverage[window.symbol]["to"]}
                        break
                    cursor.cursor_value = state
            else:
                return {"status": "partial", "pages": used}
        return {"status": "waiting_for_candles" if waiting else "completed", "pages": used}
    finally:
        lock.release(JOB)
