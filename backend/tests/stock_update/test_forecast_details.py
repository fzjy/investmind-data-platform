from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.errors import TushareRateLimitError
from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from app.db.models import FinancialForecast, JobCursor, PublishedSnapshot, SourcePayload
from app.ingestion.normalizers import normalize_financial_row
from app.ingestion.pipeline import IngestionPipeline
from app.stocks import forecast_details as detail


NOW = datetime(2026, 11, 11, tzinfo=UTC)
SYMBOLS = ("600519.SH", "688111.SH")


def sample(symbol, day):
    return dict(ts_code=symbol, ann_date=day, end_date="20260630", type="预增",
        p_change_min=8, p_change_max=10, net_profit_min=100, net_profit_max=120)


def setup(new_pg_factory):
    f = new_pg_factory()
    rows = [sample(SYMBOLS[0], "20260703"), sample(SYMBOLS[0], "20260715"), sample(SYMBOLS[1], "20260715")]
    from app.db.models import Security
    with f.begin() as s:
        s.add_all(Security(ts_code=code, symbol=code[:6], name="样本") for code in SYMBOLS)
        s.flush()
        def add(product, payload):
            s.add(PublishedSnapshot(product=product, payload=payload, as_of=NOW, version="test"))
        add("screener", {"stocks": [{"symbol": sym} for sym in SYMBOLS]})
        for sym in SYMBOLS:
            add("stock-workbench:" + sym, {"dailyChart": {"candles": [dict(date=day,open=1,high=1,low=1,close=1)
                for day in ("2026-07-01","2026-07-31")]}})
        for r in rows:
            s.add(FinancialForecast(**normalize_financial_row(r)))
    return f, rows


class Source:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def call(self, api, params, fields):
        assert api == "forecast"
        self.calls.append(params.copy())
        data = [dict(r, first_ann_date=r["ann_date"], summary="增长", change_reason="销量上升")
            for r in self.rows if r["ts_code"] == params["ts_code"] and params["start_date"] <= r["ann_date"] <= params["end_date"]]
        return TushareResponse(fields=fields, items=[tuple(r[k] for k in fields) for r in data])


def run(f, client, **options):
    args = dict(now=NOW, budget=1, policy={"retryDays":1, "maxChecks":2, "backoffMinutes":15}, symbols=SYMBOLS)
    return detail.capture(f, client, **(args | options))


def test_resume_and_preserve_details_on_old_contract_reingest(new_pg_factory):
    f, rows = setup(new_pg_factory)
    client = Source(rows)
    assert run(f, client, dry_run=True)["readyWindows"] == 2 and not client.calls
    assert run(f, client)["enrichedCount"] == 2
    assert run(f, client)["enrichedCount"] == 1
    assert run(f, client)["requestCount"] == 0
    with f.begin() as s:
        assert len(s.scalars(select(SourcePayload)).all()) == 2
        assert len(s.scalars(select(JobCursor)).all()) == 2
        plan = detail.Window(SYMBOLS[0], date(2026,7,1), date(2026,7,31)).request()
        fields = FIELD_REGISTRY[Dataset.FINANCIAL_FORECAST]
        legacy = TushareResponse(fields=fields, items=[tuple(rows[0][k] for k in fields)])
        IngestionPipeline(client, f).persist_response(plan, legacy, session=s)
    assert run(f, client)["requestCount"] == 0


def test_late_core_forecast_is_discovered_from_notice_and_does_not_repeat(new_pg_factory):
    from app.db.models import ContentDocument
    f, rows = setup(new_pg_factory)
    symbol = SYMBOLS[1]
    with f.begin() as session:
        session.add(ContentDocument(source="cninfo", content_type="announcement", external_id="n",
            dedupe_key="cninfo:n", securities=[symbol], title="某公司关于2026年半年度业绩预告的自愿性披露公告",
            published_at=datetime(2026,7,28,16,tzinfo=UTC), source_url="https://static.cninfo.com.cn/n.pdf",
            copyright_status="link_only"))
    client = Source(rows + [sample(symbol, "20260729")])
    assert run(f, client, symbols=[symbol])["enrichedCount"] == 2
    assert run(f, client, symbols=[symbol])["requestCount"] == 0
    with f() as session:
        forecast = session.scalar(select(FinancialForecast).filter_by(ts_code=symbol, ann_date=date(2026,7,29)))
        assert forecast.data["summary"] == "增长" and forecast.end_date == date(2026,6,30)


def test_empty_windows_bounded_and_single_day_limit_is_unresolved(new_pg_factory, monkeypatch):
    f, rows = setup(new_pg_factory)
    client = Source([])
    assert run(f, client, symbols=SYMBOLS[:1])["waitingRows"] == 2
    assert run(f, client, symbols=SYMBOLS[:1])["requestCount"] == 0
    assert run(f, client, symbols=SYMBOLS[:1], now=NOW + timedelta(days=1))["unresolvedRows"] == 2
    monkeypatch.setattr(detail, "ROW_LIMIT", 1)
    client.rows = rows
    for _ in range(8):
        report = run(f, client, symbols=SYMBOLS[1:])
        if not report["readyWindows"]:
            break
    assert report["unresolvedRows"] == 1 and report["enrichedCount"] == 0


def test_new_candle_does_not_reset_retry_for_same_month(new_pg_factory):
    f, _ = setup(new_pg_factory)
    symbol = SYMBOLS[1]
    def set_end(day):
        with f.begin() as session:
            base = session.get(PublishedSnapshot, "stock-workbench:" + symbol)
            base.payload = {"dailyChart":{"candles":[dict(date=d, open=1, high=1, low=1, close=1)
                                                      for d in ("2026-07-01",day)]}}
    set_end("2026-07-18")
    source = Source([])
    assert run(f, source, symbols=[symbol])["waitingRows"] == 1
    set_end("2026-07-19")
    assert run(f, source, symbols=[symbol])["requestCount"] == 0


def test_ambiguous_forecast_keeps_raw_and_allows_other_rows(new_pg_factory):
    f, rows = setup(new_pg_factory)
    ambiguous = {**rows[0], "type": "修正"}
    report = run(f, Source(rows + [ambiguous]), symbols=[SYMBOLS[0]])
    assert report["enrichedCount"] == 1 and report["waitingRows"] == 1
    with f() as session:
        assert session.scalar(select(JobCursor)).cursor_value["ambiguousVersions"] == 1
        assert session.scalar(select(SourcePayload)) is not None


def test_rate_limit_does_not_advance_and_leap_year_bound(new_pg_factory):
    f, rows = setup(new_pg_factory)
    class Limited:
        def call(self, *args):
            raise TushareRateLimitError()
    failed = run(f, Limited())
    assert failed["readyWindows"] == 2 and failed["errorCode"] == "TushareRateLimitError"
    assert run(f, Limited())["requestCount"] == 0
    assert run(f, Source(rows), now=NOW + timedelta(days=1))["enrichedCount"] == 2
    assert detail._history([{"date":"2010-01-04"},{"date":"2024-02-29"}]) == (date(2019,2,28), date(2024,2,29))


def test_routine_passes_only_remaining_financial_budget(monkeypatch):
    from backend.tests.jobs.test_routine_update import setup_steps, settings
    routine, _ = setup_steps(monkeypatch)
    monkeypatch.setattr(routine, "financial_supply_config", lambda: {"enabled": True, "maxHttpPerRun": 3, "timeoutSeconds": 20})
    monkeypatch.setattr(routine, "forecast_policy", lambda: {"enabled": True})
    monkeypatch.setattr(routine, "run_financial_step", lambda *a, **kw: {"status":"completed","publishReady":True,"requestCount":2})
    used = []
    def forecast(*args, budget, **kwargs):
        used.append(budget)
        return {"status":"pending","requestCount":budget}
    monkeypatch.setattr(routine, "run_forecast_step", forecast)
    report = routine.run_routine_update(settings(routine_macro_enabled=False), now=NOW, macro_rebuild=lambda s: None)
    assert used == [1] and report["status"] == "partial"
    assert report["httpCounts"] == {"stocks":2, "macro":0, "financial":2, "forecastDetail":1, "total":5}
