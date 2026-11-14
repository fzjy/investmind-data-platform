from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import Dividend, FinancialIndicator, MetricValue, PageSnapshot, PublishedSnapshot
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.marts.contracts import MartCandidate, MartRows
from app.marts.publication import PublicationBaseChanged, SnapshotPublisher
from app.stocks.derived.workbench_cards import CARD_IDS, metric_value_card
from app.stocks.marts.metric_cards import project_cards, rebuild_metric_cards
from backend.tests.jobs.test_build_and_publish_integration import (
    _factory, _real_build_and_publish_job, _seed_complete_core_sample, _seed_second_security,
)


SYMBOL = "600519.SH"
PRODUCT = "stock-workbench:" + SYMBOL
DAY = date(2026, 8, 24)


def prepared():
    factory = _factory()
    _seed_complete_core_sample(factory)
    _seed_second_security(factory)
    report = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(DAY))
    assert report.status == "completed", report.error
    return factory


def state(factory):
    with factory() as s:
        return {p.product: (p.version, deepcopy(p.payload), p.published_at, p.freshness_checked_at)
                for p in s.scalars(select(PublishedSnapshot))}


def revise_bps(factory, value):
    with factory.begin() as s:
        row = s.scalar(select(FinancialIndicator).where(FinancialIndicator.ts_code == SYMBOL,
            FinancialIndicator.end_date == date(2026, 6, 30)))
        if row is None:
            row = FinancialIndicator(ts_code=SYMBOL, end_date=date(2026, 6, 30), ann_date=DAY)
            s.add(row)
        row.data = {"bps": value}


def metric_rows(factory):
    with factory() as s:
        return {r.metric_id: (metric_value_card(r), r.calculated_at) for r in s.scalars(select(MetricValue).where(
            MetricValue.ts_code == SYMBOL, MetricValue.metric_id.in_(CARD_IDS), MetricValue.as_of == DAY))}


def test_late_revision_dry_run_atomic_retry_history_and_unchanged_other_products(monkeypatch):
    factory = prepared()
    before = state(factory)
    derived = metric_rows(factory)
    revise_bps(factory, 250)
    plan = rebuild_metric_cards(factory)
    assert plan["changedSymbols"] == [SYMBOL] and plan["requestCount"] == 0
    assert state(factory) == before and metric_rows(factory) == derived
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError("injected pointer failure")
        patch.setattr(SnapshotPublisher, "_switch_pointer", fail)
        with pytest.raises(RuntimeError):
            rebuild_metric_cards(factory, dry_run=False)
    assert state(factory) == before and metric_rows(factory) == derived
    assert rebuild_metric_cards(factory, dry_run=False)["promotedProducts"] == [PRODUCT]
    after = state(factory)
    for product in before.keys() - {PRODUCT}:
        assert after[product] == before[product]
    current = deepcopy(after[PRODUCT][1])
    previous = deepcopy(before[PRODUCT][1])
    rows = metric_rows(factory)
    assert current["metrics"]["cards"][:8] == [rows[k][0] for k in CARD_IDS]
    assert rows["workbenchPb"][0]["value"] == "6.00"
    for payload in (previous, current):
        metrics = payload["metrics"]
        metrics.pop("cardInputSignature")
        metrics["cards"] = [c for c in metrics["cards"] if c.get("id") not in CARD_IDS]
    assert previous == current
    assert rebuild_metric_cards(factory, dry_run=False)["status"] == "no_change"
    assert metric_rows(factory) == rows and state(factory) == after
    with factory() as s:
        old = s.scalar(select(PageSnapshot).where(PageSnapshot.product == PRODUCT,
            PageSnapshot.snapshot_version == before[PRODUCT][0]))
        assert old.payload == {k: v for k, v in before[PRODUCT][1].items() if k != "risks"}


def test_inputs_and_parent_are_rechecked_in_promotion_transaction():
    factory = prepared()
    revise_bps(factory, 240)
    def preview():
        with factory() as s:
            parent = s.get(PublishedSnapshot, PRODUCT)
            return project_cards(s, [MartCandidate(PRODUCT, parent.as_of, parent.payload,
                MartRows(workbenches=(parent.payload,)))])[0]
    obsolete = preview()
    revise_bps(factory, 300)
    with pytest.raises(PublicationBaseChanged, match="INPUT_CHANGED"):
        SnapshotPublisher(factory).publish_many((obsolete,), "old", ())
    stale_parent = preview()
    assert rebuild_metric_cards(factory, dry_run=False)["status"] == "completed"
    latest = state(factory)
    with pytest.raises(PublicationBaseChanged, match="PARENT_CHANGED"):
        SnapshotPublisher(factory).publish_many((stale_parent,), "late", ())
    assert state(factory) == latest


def test_scoped_stock_builder_retains_searchable_other_workbenches():
    from app.stocks.update.publication import StockSnapshotRepository, StockMartBuilders
    factory = prepared()
    old = state(factory)
    snapshot = StockSnapshotRepository(factory).load(DAY, [SYMBOL])
    _, workbench = StockMartBuilders(factory).build(snapshot, (SYMBOL,), "next")
    indexed = {r["symbol"]: r for r in workbench.payload["sidebar"]["stocks"]}
    assert indexed[SYMBOL]["price"] == workbench.payload["quote"]["price"]
    assert indexed["000001.SZ"] == next(r for r in old[PRODUCT][1]["sidebar"]["stocks"] if r["symbol"] == "000001.SZ")
    assert state(factory) == old


def test_daily_job_publishes_previously_visible_workbench_without_disclosures_on_payment_day():
    from dataclasses import replace
    from app.db.models import TradeCalendar, DailyBasic, DailyPrice
    from app.connectors.tushare.strategies import Dataset
    from app.stocks.jobs.daily_sync_repository import DailySyncRepository
    from backend.tests.jobs.test_daily_sync import make_job, standard_options, TEST_UNIVERSE
    factory = prepared()
    before = state(factory)
    day = date(2026,8,25)
    with factory.begin() as s:
        s.add(TradeCalendar(exchange="SSE", cal_date=day, is_open=True))
        s.add(DailyPrice(ts_code=SYMBOL, trade_date=day, close=1501, open=1500, high=1501, low=1500,
                         pre_close=1500, pct_chg=0.07, vol=110, amount=3100))
        basic = s.scalar(select(DailyBasic).where(DailyBasic.ts_code == SYMBOL, DailyBasic.trade_date == DAY))
        s.add(DailyBasic(ts_code=SYMBOL, trade_date=day, pe=basic.pe, pb=basic.pb, dv_ttm=basic.dv_ttm,
                         data=deepcopy(basic.data)))
        s.add(Dividend(ts_code=SYMBOL, end_date=date(2025,12,31), ann_date=date(2026,5,1), div_proc="实施",
            record_date=DAY, ex_date=DAY, pay_date=day, cash_div_tax=Decimal("1"), data={}))
    # 跑真实 DailySyncJob 与 BuildAndPublishJob，只替换 HTTP 传输和运行历史。
    job, runner, *_ = make_job(repository=DailySyncRepository(factory), build=_real_build_and_publish_job(factory),
                               universe=replace(TEST_UNIVERSE, symbols=(SYMBOL,)))
    runner.behaviors[Dataset.DISCLOSURE_DATE] = "raise"
    report = job.run(standard_options(business_date=day))
    assert report.status == "partial" and report.affected_symbols == (SYMBOL,), [
        q for q in report.quality if not q.passed and q.severity == "blocking"]
    assert Dataset.INCOME not in [p.dataset for p in runner.calls]
    after = state(factory)
    card = after[PRODUCT][1]["metrics"]["cards"][4]
    assert after[PRODUCT][1]["quote"]["price"] == 1501
    assert card["calculation"]["asOf"] == day.isoformat()
    assert any("已派息" in r["label"] for r in card["calculation"]["rows"])
    assert after["stock-workbench:000001.SZ"] == before["stock-workbench:000001.SZ"]
    assert {r["symbol"] for r in after["screener"][1]["stocks"]} == {SYMBOL,"000001.SZ"}


def test_postgres_source_lock_contention_is_retryable_without_waiting():
    from types import SimpleNamespace
    from sqlalchemy.exc import OperationalError
    from app.stocks.marts.metric_cards import persist_projected_cards, MetricCardSourceBusy
    class Busy(Exception):
        sqlstate = "55P03"
    class Session:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def execute(self, statement):
            assert str(statement).endswith("IN SHARE MODE NOWAIT")
            raise OperationalError(str(statement), None, Busy())
    with pytest.raises(MetricCardSourceBusy, match="BUSY_RETRY"):
        persist_projected_cards(Session(), [SimpleNamespace(metric_card_signature="pending")])


def test_unchanged_source_row_recomputed_on_settlement_day():
    factory = _factory()
    _seed_complete_core_sample(factory)
    with factory.begin() as s:
        s.add(Dividend(ts_code=SYMBOL, end_date=date(2025,12,31), ann_date=date(2026,5,1), div_proc="实施",
            record_date=DAY, ex_date=DAY, pay_date=date(2026,8,25), cash_div_tax=Decimal("1"), data={}))
    job = _real_build_and_publish_job(factory)
    for day, wording in [(DAY, "未到派息日"), (date(2026,8,25), "已派息")]:
        report = job.run(BuildAndPublishOptions(day))
        assert report.status == "completed", report.error
        card = state(factory)[PRODUCT][1]["metrics"]["cards"][4]
        assert card["calculation"]["asOf"] == day.isoformat()
        assert any(wording in r["label"] for r in card["calculation"]["rows"])
