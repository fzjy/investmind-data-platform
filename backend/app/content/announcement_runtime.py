"""在配置股票池和已发布日 K 的交集内执行有界公告更新。"""
from contextlib import ExitStack

import httpx

from app.connectors.cninfo import CninfoAnnouncements
from app.content.announcement_history import backfill_pages, history_plan
from app.db.session import create_engine_and_session
from app.shared.readonly_database import open_readonly_factory
from app.stocks.api.universe import UniverseResolver
from app.stocks.api.supply import announcement_settings


def announcement_policy():
    policy = announcement_settings()
    if type(policy["enabled"]) is not bool or type(policy["maxPagesPerRun"]) is not int or not 1 <= policy["maxPagesPerRun"] <= 30:
        raise ValueError("公告采集配置无效")
    if type(policy["revisitDays"]) is not int or not 0 <= policy["revisitDays"] <= 31:
        raise ValueError("公告回看天数须在 0 至 31 天之间")
    return policy


def run_announcements(settings, *, page_budget, dry_run=False, publish=False, window=None, revisit_days=0):
    pool = UniverseResolver().resolve(settings.sync_universe, universe_file=settings.sync_universe_file)
    symbols = {s for s in pool.symbols if s.endswith((".SH", ".SZ"))}
    if window:
        if window.symbol not in symbols:
            raise ValueError("证券不在当前沪深股票池内")
        symbols = {window.symbol}
    with ExitStack() as stack:
        if dry_run:
            factory = stack.enter_context(open_readonly_factory(settings.database_read_url))
        else:
            engine, factory = create_engine_and_session(settings.database_url)
            stack.callback(engine.dispose)
        with factory() as session:
            pending, coverage, waiting = history_plan(session, symbols, window_limit=window, revisit_days=revisit_days)
        if window and (window.symbol not in coverage or coverage[window.symbol]["from"] != window.start.isoformat()
                       or coverage[window.symbol]["to"] != window.end.isoformat()):
            raise ValueError("所选日期超出该股已发布日 K 的五年历史范围")
        if dry_run:
            return {"status": "planned", "pendingWindows": len(pending), "waitingForCandles": waiting, "requestCount": 0}
        client = stack.enter_context(httpx.Client(timeout=15))
        source = CninfoAnnouncements(client)
        result = backfill_pages(factory, symbols, source.fetch_page, page_budget, window_limit=window, revisit_days=revisit_days)
        result["requestCount"] = source.request_count
        if publish and result["status"] not in {"failed", "locked"}:
            from app.stocks.api.chart_publication import publish_forecast_charts
            result["publication"] = publish_forecast_charts(factory, symbols, include_announcements=True)
        return result
