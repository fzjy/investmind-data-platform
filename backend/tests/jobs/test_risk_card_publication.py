from copy import deepcopy

import pytest
from sqlalchemy import select

from app.db.models import IncomeStatement, PageSnapshot, PublishedSnapshot, RiskResult, StockWorkbenchSnapshot
from app.stocks.marts.risk_rebuild import rebuild_workbench_risks
from backend.tests.jobs.test_metric_card_publication import DAY, PRODUCT, SYMBOL, prepared


def test_valuation_rechecks_same_day_revisions_and_missing_incremental_trading_days(monkeypatch):
    from datetime import timedelta

    from app.db.models import DailyBasic, DailyPrice
    from app.stocks.derived.risk_inputs import load_risk_inputs
    from app.stocks.derived.workbench_risks import calculate_risks

    factory = prepared()
    history = {SYMBOL: dict(start="2016-08-24", end=DAY.isoformat(), rows={DAY.isoformat(): [20, 5]})}
    monkeypatch.setattr("app.stocks.valuation_supply.risk_valuation_history", lambda *args: history)
    def valuation(session, day):
        data = load_risk_inputs(session, (SYMBOL,), day)[SYMBOL]
        return calculate_risks(data)["groups"][1]["items"][0]

    with factory.begin() as s:
        assert valuation(s, DAY)["status"] == "normal"
        daily = s.scalar(select(DailyBasic).where(DailyBasic.ts_code == SYMBOL, DailyBasic.trade_date == DAY))
        daily.pe = 25
        revised = valuation(s, DAY)
        assert revised["status"] == "unknown"
        assert "原件与日更估值不一致" in revised["conclusion"]
        daily.pe = 20
        tomorrow = DAY + timedelta(days=1)
        s.add(DailyPrice(ts_code=SYMBOL, trade_date=tomorrow))
        missing = valuation(s, tomorrow)
        assert missing["status"] == "unknown" and tomorrow.isoformat() in missing["conclusion"]
        s.add(DailyBasic(ts_code=SYMBOL, trade_date=tomorrow, pe=26, pb=6))
        assert valuation(s, tomorrow)["status"] != "unknown"


def test_rebuild_replaces_obsolete_results_and_scrubs_both_history_tables():
    factory = prepared()
    with factory.begin() as s:
        parent = s.get(PublishedSnapshot, PRODUCT)
        initial = deepcopy(parent.payload)
        parent.payload = initial | {"risks": {"old": True}}
        page = s.scalar(select(PageSnapshot).where(PageSnapshot.product == PRODUCT))
        page.payload = {**page.payload, "risks": {"old": True}}
        history = s.scalar(select(StockWorkbenchSnapshot).where(StockWorkbenchSnapshot.ts_code == SYMBOL))
        expected_history = deepcopy(history.payload)
        history.payload = {**history.payload, "risks": {"old": True}}
        old_page_id, old_history_id = page.id, history.id
        s.add(RiskResult(ts_code="RETIRED", risk_id="old", as_of=DAY, status="unknown", formula="", conclusion=""))
    preview = rebuild_workbench_risks(factory)
    assert preview["currentSets"] == preview["pageHistory"] == preview["workbenchHistory"] == 1
    rebuild_workbench_risks(factory, execute=True)
    with factory() as s:
        current = s.get(PublishedSnapshot, PRODUCT)
        assert current.payload == initial
        assert s.get(PageSnapshot, old_page_id).payload == {k: v for k, v in initial.items() if k != "risks"}
        assert s.get(StockWorkbenchSnapshot, old_history_id).payload == expected_history
        rows = list(s.scalars(select(RiskResult).where(RiskResult.ts_code == SYMBOL)))
        assert len(rows) == 12 and all(row.snapshot_version == current.version for row in rows)
        assert s.scalar(select(RiskResult.id).where(RiskResult.ts_code == "RETIRED")) is None
    repeat = rebuild_workbench_risks(factory)
    assert repeat["currentSets"] == repeat["replacedRows"] == repeat["pageHistory"] == repeat["workbenchHistory"] == 0


def test_explicit_history_and_fallback_discard_legacy_risk_payload():
    from app.shared.repository import SnapshotRepository
    from app.stocks.database_provider import StocksDatabaseProvider
    from app.stocks.normalizer import normalize_workbench

    factory = prepared()
    with factory.begin() as s:
        pointer = s.get(PublishedSnapshot, PRODUCT)
        version, quote = pointer.version, pointer.payload["quote"]
        for model in (PageSnapshot, StockWorkbenchSnapshot):
            row = s.scalar(select(model).where(model.snapshot_version == version,
                model.product == PRODUCT if model is PageSnapshot else model.ts_code == SYMBOL))
            row.payload = {**row.payload, "risks": {"oldScore": 99}}
        s.delete(pointer)
    repository = SnapshotRepository(factory)
    provider = StocksDatabaseProvider(repository, factory)
    for snapshot in (repository.read_versioned(PRODUCT, version), provider.read_workbench(SYMBOL)):
        payload = normalize_workbench(snapshot.raw, assess_risks=True)
        assert payload["quote"] == quote
        assert payload["risks"] == dict(historical=True, level="风险清单仅提供最新结果", score=None, groups=[])


def test_source_revision_cannot_publish_stale_risk_details():
    from datetime import date
    from app.marts.contracts import MartCandidate
    from app.marts.publication import PublicationBaseChanged, SnapshotPublisher
    from app.stocks.marts.risk_cards import project_risks
    factory = prepared()
    with factory() as s:
        parent = s.get(PublishedSnapshot, PRODUCT)
        pending = project_risks(s, [MartCandidate(PRODUCT, parent.as_of, parent.payload,
            workbench_parent_version=parent.version)])
    with factory.begin() as s:
        s.add(IncomeStatement(ts_code=SYMBOL, end_date=date(2025, 12, 31), ann_date=DAY, report_type="4",
                              data={"n_income_attr_p": 100, "t_compr_income": 140}))
    with pytest.raises(PublicationBaseChanged, match="RISK_SOURCE_CHANGED"):
        SnapshotPublisher(factory).publish_many(pending, "obsolete", ())
    rebuild_workbench_risks(factory, execute=True)
    with factory() as s:
        row = s.scalar(select(RiskResult).where(RiskResult.ts_code == SYMBOL,
                                                RiskResult.risk_id == "comprehensive_income_gap"))
        assert row.status == "risk" and row.inputs["reportDate"] == "2025-12-31"


def test_announcement_only_release_persists_risk_version_and_rollback_is_atomic(monkeypatch):
    from app.marts.contracts import MartCandidate
    from app.marts.publication import SnapshotPublisher
    from app.stocks.marts.risk_cards import project_overlay_risks
    factory = prepared()
    with factory() as s:
        parent = s.get(PublishedSnapshot, PRODUCT)
        versions = {PRODUCT: parent.version}
        payload = deepcopy(parent.payload)
        payload["quote"]["latestMention"] = "本期公告"
        candidates = project_overlay_risks(s, [MartCandidate(PRODUCT, parent.as_of, payload)], versions)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise RuntimeError("测试提交失败")
        patch.setattr("app.stocks.marts.risk_cards.persist_current_risks", fail)
        with pytest.raises(RuntimeError):
            SnapshotPublisher(factory).publish_many(candidates, "risk-overlay", (), expected_versions=versions)
    with factory() as s:
        assert s.get(PublishedSnapshot, PRODUCT).version == versions[PRODUCT]
    SnapshotPublisher(factory).publish_many(candidates, "risk-overlay", (), expected_versions=versions)
    with factory() as s:
        rows = s.scalars(select(RiskResult).where(RiskResult.ts_code == SYMBOL)).all()
        assert len(rows) == 12 and {r.snapshot_version for r in rows} == {"risk-overlay"}
        historical = s.scalar(select(PageSnapshot).where(PageSnapshot.product == PRODUCT,
            PageSnapshot.snapshot_version == "risk-overlay"))
        assert "risks" not in historical.payload
