"""给已发布日 K 范围内的已知预告补详情，按证券、披露月分批续跑。"""
from calendar import monthrange
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import TushareError, TushareTransientError
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.connectors.tushare.strategies import Dataset, SyncScope, TushareMode, build_request_plans
from app.db.models import ContentDocument, FinancialForecast, JobCursor, PublishedSnapshot
from app.content.api.read_models import DISPLAY_STATUSES, forecast_period
from app.db.session import create_engine_and_session
from app.ingestion.normalizers import normalize_financial_row
from app.ingestion.repositories import IngestionRepository, content_hash
from app.jobs.locks import job_lock_for_engine
from app.shared.readonly_database import open_readonly_factory
from app.shared.trading_days import candle_dates, chart_history_bounds
from app.stocks.universes.resolver import UniverseResolver


JOB = "workbench-forecast-detail-v1"
EXTRA = ("first_ann_date", "summary", "change_reason")
ROW_LIMIT = 3500
RETRY_CURSOR = "supplier-retry"


@dataclass(frozen=True)
class Window:
    symbol: str
    start: date
    end: date
    request_start: date | None = None
    request_end: date | None = None

    @property
    def key(self):
        return f"{self.symbol}:{self.start}:{self.end}"

    def request(self):
        scope = SyncScope(symbols=(self.symbol,), start_date=self.request_start or self.start, end_date=self.request_end or self.end)
        plan, = build_request_plans(Dataset.FINANCIAL_FORECAST, TushareMode.STANDARD, scope)
        return replace(plan, fields=plan.fields + EXTRA, cursor_key=self.key)


def _history(candles):
    return chart_history_bounds([date.fromisoformat(c["date"]) for c in candles])


def _fingerprint(rows):
    return content_hash(sorted((r.id, content_hash(r.data)) for r in rows))


@dataclass
class MissingForecast:
    id: int
    ts_code: str
    ann_date: date
    end_date: date

    @property
    def data(self):
        return {"symbol": self.ts_code, "day": self.ann_date, "period": self.end_date}


def notice_targets(documents, symbols):
    zone = ZoneInfo("Asia/Shanghai")
    for row in documents:
        period = forecast_period(row.title)
        if period:
            stamp = row.published_at
            day = (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).astimezone(zone).date()
            for symbol in symbols.intersection(row.securities or ()):
                yield MissingForecast(-row.id, symbol, day, period)


def forecast_notices():
    return select(ContentDocument).where(ContentDocument.source == "cninfo",
        ContentDocument.content_type == "announcement", ContentDocument.title.contains("业绩预告"),
        ContentDocument.copyright_status.in_(DISPLAY_STATUSES))


def _pending(session, today, attempts, allowed_symbols):
    base = session.get(PublishedSnapshot, "screener")
    if base is None:
        raise ValueError("FORECAST_SCREENER_REQUIRED")
    symbols = {r["symbol"] for r in base.payload["stocks"]} & set(allowed_symbols)
    products = {"stock-workbench:" + symbol: symbol for symbol in symbols}
    candle_series = session.execute(select(PublishedSnapshot.product,
        PublishedSnapshot.payload["dailyChart"]["candles"]).where(PublishedSnapshot.product.in_(products)))
    bounds = {products[p]: chart_history_bounds(candle_dates(candles)) for p, candles in candle_series}
    groups = defaultdict(list)
    known = list(session.scalars(select(FinancialForecast).where(FinancialForecast.ts_code.in_(symbols))))
    keys = {(r.ts_code, r.ann_date, r.end_date) for r in known}
    candidates = list(known)
    for target in notice_targets(session.scalars(forecast_notices()), symbols):
        key = (target.ts_code, target.ann_date, target.end_date)
        if key not in keys:
            candidates.append(target)
            keys.add(key)
    for r in candidates:
        span = bounds.get(r.ts_code)
        if not span or not r.ann_date or not span[0] <= r.ann_date <= span[1] or all(r.data.get(f) not in (None, "") for f in EXTRA):
            continue
        first = r.ann_date.replace(day=1)
        last = first.replace(day=monthrange(first.year, first.month)[1])
        groups[Window(r.ts_code, first, last)].append(r)
    states = {c.cursor_key: c.cursor_value for c in session.scalars(select(JobCursor).where(JobCursor.job_name == JOB))}
    ready = []
    waiting = exhausted = 0
    while groups:
        window, rows = groups.popitem()
        state = states.get(window.key, {})
        if state.get("status") == "split" and window.start < window.end:
            mid = window.start + (window.end - window.start) // 2
            for r in rows:
                child = Window(window.symbol, window.start, mid) if r.ann_date <= mid else Window(window.symbol, mid + timedelta(days=1), window.end)
                groups[child].append(r)
        elif state.get("signature") == _fingerprint(rows) and state.get("checks", 0) >= attempts:
            exhausted += len(rows)
        elif state.get("signature") == _fingerprint(rows) and state.get("nextCheck", "") > today.isoformat():
            waiting += len(rows)
        else:
            start, end = bounds[window.symbol]
            ready.append((replace(window, request_start=max(start, window.start), request_end=min(end, window.end)), [r.id for r in rows]))
    ready.sort(key=lambda item: (-item[0].end.toordinal(), item[0].symbol))
    return ready, waiting, exhausted


def _store(session, window, ids, response, now, policy):
    plan = window.request()
    if len(response.fields) != len(plan.fields) or set(response.fields) != set(plan.fields):
        raise ValueError("FORECAST_FIELDS_INVALID")
    by_period = {}
    for row in response.rows():
        normalized = normalize_financial_row(row)
        ann, end = normalized["ann_date"], normalized["end_date"]
        if row["ts_code"] != window.symbol or not ann or not end or not (window.request_start or window.start) <= ann <= (window.request_end or window.end):
            raise ValueError("FORECAST_RESPONSE_OUTSIDE_WINDOW")
        key = ann, end
        if key in by_period and by_period[key] != normalized["data"]:
            by_period[key] = None
        else:
            by_period[key] = normalized["data"]
    repo = IngestionRepository(session)
    batch = repo.start_batch(JOB, "standard")
    repo.save_payload(batch, plan.api_name, plan.params, plan.fields, response)
    rows = list(session.scalars(select(FinancialForecast).where(FinancialForecast.id.in_(ids)).with_for_update()))
    cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == window.key))
    if cursor is None:
        cursor = JobCursor(job_name=JOB, cursor_key=window.key, cursor_value={})
        session.add(cursor)
    previous = cursor.cursor_value
    unresolved = []
    truncated = len(response.items) >= ROW_LIMIT
    missing_ids = [-i for i in ids if i < 0]
    if missing_ids:
        docs = session.scalars(forecast_notices().where(ContentDocument.id.in_(missing_ids)))
        for target in notice_targets(docs, {window.symbol}):
            found = by_period.get((target.ann_date, target.end_date))
            if truncated or found is None:
                unresolved.append(target)
                continue
            existing = session.scalar(select(FinancialForecast).filter_by(ts_code=window.symbol,
                ann_date=target.ann_date, end_date=target.end_date).with_for_update())
            if existing is None:
                existing = FinancialForecast(**normalize_financial_row(found))
                session.add(existing)
                session.flush()
            rows.append(existing)
    for r in rows:
        found = by_period.get((r.ann_date, r.end_date))
        if truncated or found is None or any(r.data.get(k) != found[k] for k in plan.fields if k not in EXTRA):
            unresolved.append(r)
        else:
            r.data = {**r.data, **{k: found[k] if found[k] not in (None, "") else r.data.get(k) for k in EXTRA}}
            if any(r.data[k] in (None, "") for k in EXTRA):
                unresolved.append(r)
    signature = _fingerprint(unresolved)
    checks = previous.get("checks", 0) if previous.get("signature") == signature else 0
    status = "matched" if not unresolved else "unmatched" if response.items else "empty"
    if truncated:
        status = "split" if window.start < window.end else "single_day_truncated"
    cursor.cursor_value = dict(status=status, batchId=batch.id, signature=signature,
        ambiguousVersions=sum(value is None for value in by_period.values()),
        checks=policy["maxChecks"] if status == "single_day_truncated" else checks + 1,
        nextCheck=(now.date() + timedelta(days=policy["retryDays"])).isoformat())
    batch.row_count = len(response.items)
    batch.request_count = 1
    batch.status = "completed"
    batch.finished_at = now
    return len(ids) - len(unresolved)


def capture(factory, client, *, now: datetime, budget: int, policy: dict, symbols, dry_run=False):
    if type(budget) is not int or budget < 0:
        raise ValueError("FORECAST_BUDGET_INVALID")
    lock = job_lock_for_engine(factory.kw["bind"])
    report = dict(requestCount=0, enrichedCount=0)
    if not dry_run and not lock.try_acquire(JOB):
        return dict(**report, status="locked")
    try:
        def pending():
            with factory() as session:
                return _pending(session, now.date(), policy["maxChecks"], symbols)

        ready, waiting, exhausted = pending()
        failure = None
        with factory() as session:
            cooldown = session.scalar(select(JobCursor).filter_by(job_name=JOB, cursor_key=RETRY_CURSOR))
            retry_at = cooldown.cursor_value["retryAt"] if cooldown else None
        delayed = bool(retry_at and now < datetime.fromisoformat(retry_at))
        if not dry_run and not delayed:
            for window, ids in ready[:budget]:
                if client is None:
                    failure = "FORECAST_TOKEN_REQUIRED"
                    break
                plan = window.request()
                report["requestCount"] += 1
                try:
                    response = client.call(plan.api_name, plan.params, plan.fields)
                    with factory.begin() as session:
                        matched = _store(session, window, ids, response, now, policy)
                    report["enrichedCount"] += matched
                except (TushareError, ValueError) as exc:
                    failure = type(exc).__name__
                    if isinstance(exc, TushareTransientError):
                        retry_at = (now + timedelta(minutes=policy["backoffMinutes"])).isoformat()
                        with factory.begin() as session:
                            IngestionRepository(session).upsert(JobCursor, {"job_name":JOB, "cursor_key":RETRY_CURSOR},
                                {"cursor_value":{"retryAt":retry_at, "error":failure}})
                    break
            if report["requestCount"]:
                ready, waiting, exhausted = pending()
        status = "planned" if dry_run else "pending" if ready or waiting or exhausted else "completed"
        report.update(status="failed" if failure else status, readyWindows=len(ready),
            nextWindow=ready[0][0].key if ready else None, waitingRows=waiting, unresolvedRows=exhausted)
        if failure:
            report["errorCode"] = failure
        if retry_at and now < datetime.fromisoformat(retry_at):
            report["retryAt"] = retry_at
        return report
    finally:
        if not dry_run:
            lock.release(JOB)


def run_forecast_step(settings, *, now, dry_run, budget, policy, publish_requested=False):
    scope = UniverseResolver().resolve(settings.sync_universe, universe_file=settings.sync_universe_file)
    with ExitStack() as stack:
        if dry_run:
            factory = stack.enter_context(open_readonly_factory(settings.database_url))
        else:
            engine, factory = create_engine_and_session(settings.database_url)
            stack.callback(engine.dispose)
        client = None
        if not dry_run and settings.tushare_token and budget:
            client = stack.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
                max_retries=0, timeout_seconds=policy["timeoutSeconds"],
                rate_limiter=RequestRateLimiter(settings.tushare_standard_requests_per_minute)))
        result = capture(factory, client, now=now, budget=budget, policy=policy, symbols=scope.symbols, dry_run=dry_run)
        if publish_requested and not dry_run and result["status"] in {"pending", "completed"}:
            from app.stocks.forecast_events import publish_forecast_charts
            result["publication"] = publish_forecast_charts(factory, scope.symbols)
            if result["publication"]["status"] == "locked":
                result["status"] = "locked"
        return result
