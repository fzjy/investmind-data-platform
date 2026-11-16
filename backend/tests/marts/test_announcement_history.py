from datetime import date, datetime, timezone

from sqlalchemy import select

from app.connectors.external_files.contracts import ContentImportRecord
from app.content.announcement_history import AnnouncementPage, backfill_pages, history_plan
from app.db.models import ContentDocument, PublishedSnapshot
from app.shared.trading_days import chart_history_bounds


SYMBOL = "600519.SH"


def publish_candles(factory, days):
    candles = [dict(date=day, open=1, high=1, low=1, close=1) for day in days]
    product = "stock-workbench:" + SYMBOL
    with factory.begin() as session:
        row = session.get(PublishedSnapshot, product)
        if row is None:
            row = PublishedSnapshot(product=product, version="test", as_of=datetime.now(timezone.utc))
            session.add(row)
        row.payload = {"dailyChart": {"candles": candles}}


def notice(identity):
    return ContentImportRecord(schemaVersion="1.0", recordType="content", source="cninfo",
        externalId=identity, contentType="announcement", title="同一公告标题", publishedAt="2026-07-04T02:00:00Z",
        sourceUrl=f"https://static.cninfo.com.cn/{identity}.PDF", securities=[SYMBOL], copyrightStatus="link_only")


def test_short_history_and_leap_year():
    last = date(2024, 2, 29)
    assert chart_history_bounds([date(2012, 1, 1), last]) == (date(2019, 2, 28), last)
    assert chart_history_bounds([date(2022, 1, 1), last]) == (date(2022, 1, 1), last)


def test_retry_second_page_and_extend_only_unchecked_days(pg_factory):
    factory = pg_factory
    publish_candles(factory, ["2026-07-01", "2026-07-10"])
    tokens = []

    def fetch(window, token):
        tokens.append(token)
        if not token:
            return AnnouncementPage((notice("first"),), 2, "second-page")
        if len(tokens) == 2:
            raise TimeoutError
        return AnnouncementPage((notice("first"), notice("second")), 2, None)

    assert backfill_pages(factory, [SYMBOL], fetch, 1)["status"] == "partial"
    assert backfill_pages(factory, [SYMBOL], fetch, 1)["status"] == "failed"
    with factory() as session:
        pending, summary, waiting = history_plan(session, [SYMBOL, "600000.SH"])
        assert pending[0][1] == "failed" and summary[SYMBOL]["checkedDays"] == 0
        assert waiting == ["600000.SH"] and not session.scalars(select(ContentDocument)).all()
    assert backfill_pages(factory, [SYMBOL], fetch, 1)["status"] == "completed"
    assert backfill_pages(factory, [SYMBOL], fetch, 1)["pages"] == 0
    assert tokens == [None, "second-page", "second-page"]
    with factory() as session:
        assert set(session.scalars(select(ContentDocument.external_id))) == {"first", "second"}
    publish_candles(factory, ["2026-06-29", "2026-07-14"])
    with factory() as session:
        pending, summary, _ = history_plan(session, [SYMBOL])
        assert [w.key for w, _ in pending] == [f"{SYMBOL}:2026-07-11:2026-07-14", f"{SYMBOL}:2026-06-29:2026-06-30"]
        assert summary[SYMBOL]["checkedDays"] == 10


def test_truncated_final_page_does_not_claim_coverage_and_empty_window_can_finish(pg_factory):
    factory = pg_factory
    publish_candles(factory, ["2026-07-04"])
    short = lambda window, token: AnnouncementPage((notice("only"),), 2, None)
    assert backfill_pages(factory, [SYMBOL], short, 1)["status"] == "failed"
    with factory() as session:
        assert history_plan(session, [SYMBOL])[1][SYMBOL]["checkedDays"] == 0
    empty = lambda window, token: AnnouncementPage((), 0, None)
    assert backfill_pages(factory, [SYMBOL], empty, 1)["status"] == "completed"
    with factory() as session:
        summary = history_plan(session, [SYMBOL])[1][SYMBOL]
        assert summary["checkedDays"] == 1 and summary["checkedEmptyWindows"] == 1
        assert session.scalar(select(ContentDocument)) is None


def test_sample_covers_only_one_day_and_revisits_when_new_prices_arrive(pg_factory):
    from app.content.announcement_history import Window
    factory = pg_factory
    day = date(2026, 7, 4)
    publish_candles(factory, ["2026-07-01", "2026-07-04"])
    called = []
    def fetch(window, token):
        called.append(window)
        return AnnouncementPage((), 0, None)
    window = Window(SYMBOL, day, day)
    for _ in range(2):
        backfill_pages(factory, [SYMBOL], fetch, 1, window_limit=window)
    assert called == [window]
    publish_candles(factory, ["2026-07-01", "2026-07-05"])
    backfill_pages(factory, [SYMBOL], fetch, 3, revisit_days=7)
    assert called.count(window) == 2
    assert backfill_pages(factory, [SYMBOL], fetch, 3, revisit_days=7)["pages"] == 0


def test_routine_only_runs_announcements_when_selected(monkeypatch):
    from backend.tests.jobs.test_routine_update import settings, setup_steps
    routine, _ = setup_steps(monkeypatch)
    calls = []
    def run(settings, **kwargs):
        calls.append(kwargs)
        return {"status": "partial", "requestCount": 2}
    monkeypatch.setattr(routine, "run_announcements", run)
    routine.run_routine_update(settings(), macro_rebuild=lambda settings: None)
    assert calls == []
    monkeypatch.setattr(routine, "announcement_policy", lambda: {"enabled": True, "maxPagesPerRun": 1, "revisitDays": 7})
    result = routine.run_routine_update(settings(), macro_rebuild=lambda settings: None)
    assert calls == [{"page_budget":1, "revisit_days":7, "dry_run":False, "publish":False}]
    assert result["status"] == "partial" and result["httpCounts"]["announcements"] == 2 and result["httpCounts"]["total"] == 7
