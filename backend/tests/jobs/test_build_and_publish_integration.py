from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    BalanceSheet,
    CashflowStatement,
    DailyBasic,
    DailyPrice,
    FinancialIndicator,
    IncomeStatement,
    PageSnapshot,
    PublishedSnapshot,
    MacroSeriesState,
    RiskResult,
    Security,
)
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.stocks.derived.pipeline import DerivedPipeline
from app.jobs.build_and_publish import (
    BuildAndPublishJob,
    BuildAndPublishOptions,
    DefaultMartBuilders,
)
from app.jobs.build_history import SqlBuildHistory
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.marts.dto import validate_product_payload
from backend.tests.test_screener import default_request




def _seed_complete_core_sample(factory: sessionmaker, *, ts_code="600519.SH", name="贵州茅台", board=None) -> None:
    with factory.begin() as session:
        session.add(
            Security(
                ts_code=ts_code,
                symbol=ts_code[:6],
                name=name,
                board=board,
                exchange="SSE",
                status="L",
                list_date=date(2001, 8, 27),
                data={"industry": "食品饮料", "region": "贵州"},
            )
        )
        session.flush()
        session.add(
            DailyPrice(
                ts_code=ts_code,
                trade_date=date(2026, 8, 24),
                open=Decimal("1490"),
                high=Decimal("1510"),
                low=Decimal("1480"),
                close=Decimal("1500"),
                pct_chg=Decimal("1.2"),
                vol=Decimal("100"),
                amount=Decimal("3000"),
            )
        )
        session.add(
            DailyBasic(
                ts_code=ts_code,
                trade_date=date(2026, 8, 24),
                pe=Decimal("20"),
                pb=Decimal("5"),
                dv_ttm=Decimal("3"),
                data={
                    "total_share": 100,
                    "float_share": 80,
                    "limit_status": "U",
                },
            )
        )
        session.add_all(
            [
                IncomeStatement(
                    ts_code=ts_code,
                    end_date=date(2025, 9, 30),
                    ann_date=date(2025, 10, 20),
                    report_type="1",
                    data={
                        "n_income_attr_p": 30,
                        "total_revenue": 300,
                        "revenue": 300,
                        "ebit": 40,
                    },
                ),
                IncomeStatement(
                    ts_code=ts_code,
                    end_date=date(2024, 12, 31),
                    ann_date=date(2025, 3, 20),
                    report_type="1",
                    data={
                        "n_income_attr_p": 100,
                        "total_revenue": 1000,
                        "revenue": 1000,
                        "ebit": 130,
                    },
                ),
                IncomeStatement(
                    ts_code=ts_code,
                    end_date=date(2024, 9, 30),
                    ann_date=date(2024, 10, 20),
                    report_type="1",
                    data={
                        "n_income_attr_p": 20,
                        "total_revenue": 200,
                        "revenue": 200,
                        "ebit": 25,
                    },
                ),
                BalanceSheet(
                    ts_code=ts_code,
                    end_date=date(2025, 12, 31),
                    ann_date=date(2026, 2, 20),
                    report_type="1",
                    data={
                        "money_cap": 200,
                        "inventories": 50,
                        "goodwill": 20,
                        "total_assets": 1000,
                        "total_hldr_eqy_exc_min_int": 700,
                        "st_borr": 50,
                        "lt_borr": 50,
                    },
                ),
                CashflowStatement(
                    ts_code=ts_code,
                    end_date=date(2025, 9, 30),
                    ann_date=date(2025, 10, 20),
                    report_type="1",
                    data={"n_cashflow_act": 30, "c_pay_acq_const_fiolta": 5},
                ),
                CashflowStatement(
                    ts_code=ts_code,
                    end_date=date(2024, 12, 31),
                    ann_date=date(2025, 3, 20),
                    report_type="1",
                    data={"n_cashflow_act": 100, "c_pay_acq_const_fiolta": 15},
                ),
                CashflowStatement(
                    ts_code=ts_code,
                    end_date=date(2024, 9, 30),
                    ann_date=date(2024, 10, 20),
                    report_type="1",
                    data={"n_cashflow_act": 20, "c_pay_acq_const_fiolta": 4},
                ),
                FinancialIndicator(
                    ts_code=ts_code,
                    end_date=date(2025, 12, 31),
                    ann_date=date(2026, 2, 20),
                    report_type="1",
                    data={
                        "invest_capital": 500,
                        "tax_to_ebt": 0.2,
                        "netdebt": 100,
                        "roe": 20,
                        "grossprofit_margin": 90,
                        "netprofit_margin": 30,
                    },
                ),
            ]
        )


def _seed_second_security(factory: sessionmaker) -> None:
    with factory.begin() as session:
        session.add(
            Security(
                ts_code="000001.SZ",
                symbol="000001",
                name="平安银行",
                exchange="SZSE",
                status="L",
                list_date=date(1991, 4, 3),
                data={"industry": "银行", "region": "深圳"},
            )
        )
        session.flush()
        session.add_all(
            (
                DailyPrice(
                    ts_code="000001.SZ",
                    trade_date=date(2026, 8, 24),
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    pct_chg=Decimal("0.5"),
                    vol=Decimal("200"),
                    amount=Decimal("4000"),
                ),
                DailyBasic(
                    ts_code="000001.SZ",
                    trade_date=date(2026, 8, 24),
                    pe=Decimal("8"),
                    pb=Decimal("1"),
                    dv_ttm=Decimal("2"),
                    data={"total_share": 200, "float_share": 180},
                ),
            )
        )


class RecordingQualityGate(QualityGate):
    def __init__(self) -> None:
        self.contexts = []

    def evaluate(self, candidates, context):
        self.contexts.append(context)
        return super().evaluate(candidates, context)


def _real_build_and_publish_job(
    factory: sessionmaker,
    *,
    quality: QualityGate | None = None,
) -> BuildAndPublishJob:
    return BuildAndPublishJob(
        repository=CoreSnapshotRepository(factory),
        derived=DerivedPipeline(factory),
        builders=DefaultMartBuilders(factory),
        quality=quality or QualityGate(),
        publisher=SnapshotPublisher(factory),
        history=SqlBuildHistory(factory),
    )


def test_securities_without_macro_rows_publish_and_macro_api_stays_unpublished(pg_factory, monkeypatch) -> None:
    factory = pg_factory
    _seed_complete_core_sample(factory)
    # 股东投影以已发布证券池为输入；先建立稳定范围，避免首轮发布本身改变下一轮输入。
    with factory.begin() as session:
        session.add(PublishedSnapshot(product="screener", version="scope-seed",
            as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
            payload={"stocks": [{"symbol": "600519.SH"}]}))

    job = _real_build_and_publish_job(factory)

    first = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))
    second = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert first.status == second.status == "completed"
    assert first.version == second.version
    assert "macro-transmission" not in first.products
    assert "macro-insights" not in first.products
    with factory() as session:
        for product in ("macro-transmission", "macro-insights"):
            macro = session.get(PublishedSnapshot, product)
            assert macro is None
        products = set(session.scalars(select(PublishedSnapshot.product)).all())
        assert products == {
            "market-overview",
            "market-explorer",
            "screener",
            "portfolio",
            "stock-workbench:600519.SH",
        }
        assert session.scalar(select(func.count(PageSnapshot.id))) == 5
        # Builder audit/risk rows are not publication pointers.
        assert session.scalar(select(func.count(RiskResult.id))) == 12


    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(
            data_backend="database",
            database_url=factory.kw["bind"].url.render_as_string(hide_password=False),
        ),
    )
    client = TestClient(main_module.create_app())
    responses = {
        "market-overview": client.get("/api/data/market-overview"),
        "market-explorer": client.get("/api/data/market-explorer"),
        "macro-transmission": client.get("/api/data/macro-transmission"),
        "macro-insights": client.get("/api/data/macro-insights"),
        "portfolio": client.get("/api/data/portfolio"),
        "stock-workbench:600519.SH": client.get("/api/data/stocks/600519.SH"),
        "screener": client.post("/api/data/screener", json=default_request()),
    }
    versions = set()
    for product, response in responses.items():
        if product in {"macro-transmission", "macro-insights"}:
            assert response.status_code == 503
            continue
        assert response.status_code == 200, (product, response.text)
        body = response.json()
        versions.add(body["meta"]["version"])
        payload = body["data"]
        if product == "screener":
            payload = {
                "bootstrap": payload["bootstrap"],
                "stocks": payload["items"],
            }
        assert validate_product_payload(product, payload) == ()
    assert versions == {first.version}
    workbench = responses["stock-workbench:600519.SH"].json()["data"]
    from app.stocks.derived.workbench_cards import CARD_IDS
    cards = workbench["metrics"]["cards"][:8]
    assert [card["id"] for card in cards] == list(CARD_IDS)
    assert all(card["calculation"]["asOf"] == "2026-08-24" for card in cards)
    assert workbench["quote"]["price"] == 1500
    assert workbench["dailyChart"]["candles"]
    assert workbench["financial"]["statements"]["tables"]


def test_full_build_reads_macro_state_metadata(pg_factory) -> None:
    factory = pg_factory
    _seed_complete_core_sample(factory)
    with factory.begin() as session:
        session.add(MacroSeriesState(
            series_id="cn.cpi.yoy", catalog_version="1.0",
            last_checked_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
            last_success_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
            active_source="tushare", fallback_used=False,
            stale=True, stale_reason="source_failed", consecutive_failures=1,
        ))
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))
    candidates = DefaultMartBuilders(factory).build(snapshot, ("600519.SH",), "candidate")
    macro = next(item for item in candidates if item.product == "macro-insights")
    status = next(item for item in macro.payload["seriesStatus"] if item["id"] == "cn.cpi.yoy")
    assert status["staleReason"] == "source_failed"
    assert status["lastCheckedAt"] == "2026-08-23T00:00:00+00:00"


def test_full_build_blocks_empty_macro_and_still_bootstraps_securities(pg_factory) -> None:
    from app.bootstrap import SnapshotReadinessChecker
    factory = pg_factory
    _seed_complete_core_sample(factory)
    report = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))
    assert report.status == "completed"
    with factory() as session:
        assert session.get(PublishedSnapshot, "macro-insights") is None
        assert session.get(PublishedSnapshot, "macro-transmission") is None
        assert session.get(PublishedSnapshot, "market-overview") is not None
    assert SnapshotReadinessChecker(factory).check(("600519.SH",)).ready
    assert any(result.name == "macro-coverage-at-least-80" and not result.passed for result in report.quality)


def test_full_build_preserves_old_macro_when_new_candidate_fails_quality(pg_factory) -> None:
    factory = pg_factory
    _seed_complete_core_sample(factory)
    old_as_of = datetime(2026, 8, 23, tzinfo=timezone.utc)
    from app.marts.contracts import MartCandidate
    SnapshotPublisher(factory).publish_many(tuple(MartCandidate(product, old_as_of, {"old": True}) for product in ("macro-transmission", "macro-insights")), "old-macro", ())
    report = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))
    assert report.status == "completed"
    with factory() as session:
        assert session.get(PublishedSnapshot, "macro-insights").version == "old-macro"
        assert session.get(PublishedSnapshot, "macro-transmission").version == "old-macro"
        assert session.get(PublishedSnapshot, "market-overview").version != "old-macro"


def test_build_without_symbol_scope_publishes_all_active_workbenches(pg_factory) -> None:
    factory = pg_factory
    _seed_complete_core_sample(factory)

    report = _real_build_and_publish_job(factory).run(
        BuildAndPublishOptions(date(2026, 8, 24))
    )

    assert report.status == "completed"
    assert "stock-workbench:600519.SH" in report.products
    with factory() as session:
        assert session.get(PublishedSnapshot, "stock-workbench:600519.SH") is not None


def test_explicit_empty_workbench_scope_builds_only_global_products(pg_factory) -> None:
    factory = pg_factory
    _seed_complete_core_sample(factory)
    quality = RecordingQualityGate()

    report = _real_build_and_publish_job(factory, quality=quality).run(
        BuildAndPublishOptions(
            as_of=date(2026, 9, 2),
            symbols=(),
            workbench_symbols=(),
        )
    )

    assert set(report.products) == {
        "market-overview",
        "market-explorer",
        "screener",
        "portfolio",
    }
    assert quality.contexts[0].requested_workbenches == ()


def test_explicit_workbench_scope_keeps_global_snapshot_full_market(pg_factory) -> None:
    factory = pg_factory
    _seed_complete_core_sample(factory)
    _seed_second_security(factory)
    quality = RecordingQualityGate()

    report = _real_build_and_publish_job(factory, quality=quality).run(
        BuildAndPublishOptions(
            as_of=date(2026, 9, 2),
            symbols=(),
            workbench_symbols=("600519.SH",),
        )
    )

    assert "stock-workbench:600519.SH" in report.products
    assert "stock-workbench:000001.SZ" not in report.products
    assert report.symbols == ("600519.SH",)
    assert "market-overview" in report.products
    assert quality.contexts[0].eligible_security_count == 2
    assert quality.contexts[0].requested_workbenches == ("600519.SH",)
    with factory() as session:
        screener = session.get(PublishedSnapshot, "screener")
        other_workbench = session.get(PublishedSnapshot, "stock-workbench:000001.SZ")
    assert screener is not None
    assert {
        item["symbol"] for item in screener.payload["stocks"]
    } == {"000001.SZ", "600519.SH"}
    assert other_workbench is None
import app.main as main_module
from app.config import Settings
