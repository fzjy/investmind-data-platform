from __future__ import annotations

import importlib
import json
import os
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

import app.cli.main as cli_module
import app.config as config_module
from app.bootstrap import BootstrapOptions, BootstrapRunner, SnapshotReadinessChecker
from app.connectors.tushare.client import TushareClient
from app.db.models import (
    AdjustmentFactor,
    BalanceSheet,
    CashflowStatement,
    DailyBasic,
    DailyPrice,
    FinancialIndicator,
    IncomeStatement,
    IngestionBatch,
    JobCursor,
    JobFailure,
    JobRun,
    MetricValue,
    PageSnapshot,
    PublishedSnapshot,
    RejectedRecord,
    RiskResult,
    Security,
    SourcePayload,
)
from app.db.session import create_engine_and_session
from app.marts.dto import validate_product_payload
from backend.tests.e2e.controlled_tushare import (
    AS_OF,
    EXPECTED_FIELDS_BY_API,
    ROWS_BY_API,
    SYMBOL,
    TEST_TOKEN,
    ControlledTushare,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_PRODUCTS = {
    "market-overview",
    "market-explorer",
    "screener",
    "portfolio",
    f"stock-workbench:{SYMBOL}",
}
EXPECTED_METRICS = {
    "realMoneyIndex": Decimal("1.0000"),
    "freeCashFlow": Decimal("94.0000"),
    "roic": Decimal("0.0640"),
    "pePercentile": Decimal("100.0000"),
    "pbPercentile": Decimal("100.0000"),
    "netDebt": Decimal("-100.0000"),
    "profitGrowth": Decimal("50.0000"),
    "revenueGrowth": Decimal("50.0000"),
    "cashflowGrowth": Decimal("50.0000"),
}
EXPECTED_RISKS = {
    "high_cash_high_debt": "normal",
    "goodwill_risk": "normal",
    "profit_structure_non_operating_dependency": "unknown",
    "impairment_pressure": "unknown",
    "comprehensive_income_gap": "unknown",
    "eps_quality_gap": "unknown",
    "equity_cash_support": "unknown",
    "valuation_band": "unknown",
    "interest_debt_burden": "unknown",
    "dividend_vs_financing": "unknown",
    "dividend_sustainability": "unknown",
    "earnings_quality_non_recurring": "unknown",
}

RUN_POSTGRES = pytest.mark.skipif(
    os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
    reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL tests",
)


class InProcessCliCommands:
    def __init__(self) -> None:
        self.results = []

    def run(self, args: tuple[str, ...]) -> int:
        result = CliRunner().invoke(cli_module.app, list(args))
        self.results.append(result)
        return result.exit_code


def test_controlled_tushare_enforces_contract_and_paginates() -> None:
    source = ControlledTushare()
    with TushareClient(
        TEST_TOKEN,
        transport=httpx.MockTransport(source.handle),
        max_retries=0,
    ) as client:
        page = client.call(
            "daily",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "2",
            },
            EXPECTED_FIELDS_BY_API["daily"],
        )
        assert page.rows() == (
            {
                "ts_code": SYMBOL,
                "trade_date": "20260824",
                "open": "1490",
                "high": "1510",
                "low": "1480",
                "close": "1500",
                "pre_close": "1490",
                "change": "10",
                "pct_chg": "0.6711",
                "vol": "100",
                "amount": "3000",
            },
        )

        with pytest.raises(AssertionError, match="unexpected fields for daily"):
            client.call(
                "daily",
                {
                    "ts_code": SYMBOL,
                    "start_date": "20230824",
                    "end_date": "20260824",
                    "limit": "2",
                    "offset": "0",
                },
                ("ts_code",),
            )

    assert [request[0] for request in source.requests] == ["daily"]


def test_controlled_tushare_rejects_fixed_row_missing_requested_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete_row = dict(ROWS_BY_API["daily"][0])
    del incomplete_row["close"]
    monkeypatch.setitem(ROWS_BY_API, "daily", (incomplete_row,))
    source = ControlledTushare()

    with TushareClient(
        TEST_TOKEN,
        transport=httpx.MockTransport(source.handle),
        max_retries=0,
    ) as client, pytest.raises(AssertionError, match="missing requested fields"):
        client.call(
            "daily",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["daily"],
        )


@pytest.mark.postgres
@RUN_POSTGRES
def test_bootstrap_reaches_all_database_apis_and_skips_second_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        source = ControlledTushare()
        main_module = _configure_environment(
            monkeypatch,
            tmp_path,
            database_url,
        )

        def controlled_client(token: str, **_kwargs: object) -> TushareClient:
            assert token == TEST_TOKEN
            return TushareClient(
                token,
                transport=httpx.MockTransport(source.handle),
                max_retries=0,
            )

        monkeypatch.setattr(cli_module, "TushareClient", controlled_client)
        engine, factory = create_engine_and_session(database_url)
        commands = InProcessCliCommands()
        bootstrap = BootstrapRunner(SnapshotReadinessChecker(factory), commands)
        options = BootstrapOptions((SYMBOL,), AS_OF, False, "standard")
        try:
            first = bootstrap.run(options)
            request_count = len(source.requests)
            second = bootstrap.run(options)
        finally:
            engine.dispose()

        assert first.status == "completed"
        assert second.status == "skipped"
        assert second.version == first.version
        assert len(source.requests) == request_count
        assert len(commands.results) == 2
        backfill_payload = json.loads(commands.results[0].stdout)
        build_payload = json.loads(commands.results[1].stdout)
        assert main_module is not None
        assert backfill_payload["status"] == "completed"
        assert backfill_payload["planned"] == 8
        assert backfill_payload["completed"] == 8
        assert backfill_payload["failed"] == 0
        assert backfill_payload["rejected"] == 0
        assert backfill_payload["batches"]
        assert all(
            batch["rejectedCount"] == 0 for batch in backfill_payload["batches"]
        )
        _assert_backfill_layers(database_url)
        assert build_payload["status"] == "completed"
        version = str(build_payload["version"])
        assert version
        assert set(build_payload["products"]) == EXPECTED_PRODUCTS
        assert build_payload["metricCount"] == 14
        assert build_payload["riskCount"] == 12
        _assert_published_layers(
            database_url,
            version,
            metric_count=int(build_payload["metricCount"]),
            risk_count=int(build_payload["riskCount"]),
        )
        _request_database_apis(
            monkeypatch,
            main_module,
            database_url,
            version,
        )
        assert source.requests == _expected_tushare_requests()


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
) -> ModuleType:
    monkeypatch.setattr(
        config_module,
        "PROJECT_ENV_FILE",
        tmp_path / "missing-project.env",
    )
    monkeypatch.setenv("DANG_DATA_BACKEND", "database")
    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(
        main_module,
        "settings",
        config_module.Settings(data_backend="database", database_url=database_url, database_read_url=database_url),
    )

    monkeypatch.setenv("DANG_DATA_BACKEND", "database")
    monkeypatch.setenv("DANG_DATABASE_URL", database_url)
    monkeypatch.setenv("DANG_DATABASE_READ_URL", database_url)
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DANG_TUSHARE_PAGE_SIZE", "2")
    monkeypatch.chdir(PROJECT_ROOT)

    command.upgrade(
        Config(str(PROJECT_ROOT / "backend" / "alembic.ini")),
        "head",
    )
    return main_module


def _assert_backfill_layers(database_url: str) -> None:
    engine = create_engine(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            assert session.scalar(select(func.count(Security.ts_code))) == 1
            assert session.scalar(select(func.count(DailyPrice.id))) == 3
            assert session.scalar(select(func.count(DailyBasic.id))) == 1
            assert session.scalar(select(func.count(AdjustmentFactor.id))) == 3
            assert session.scalar(select(func.count(IncomeStatement.id))) == 3
            assert session.scalar(select(func.count(BalanceSheet.id))) == 1
            assert session.scalar(select(func.count(CashflowStatement.id))) == 3
            assert session.scalar(select(func.count(FinancialIndicator.id))) == 1
            assert session.scalar(select(func.count(IngestionBatch.id))) == 12
            assert session.scalar(select(func.count(SourcePayload.id))) == 12
            assert session.scalar(select(func.count(RejectedRecord.id))) == 0
            assert session.scalar(select(func.count(JobFailure.id))) == 0
            assert session.scalar(select(func.count(JobCursor.id))) == 0
            completed_backfills = session.scalar(
                select(func.count(JobRun.id)).where(
                    JobRun.job_name == "backfill",
                    JobRun.status == "completed",
                )
            )
            assert completed_backfills == 1
    finally:
        engine.dispose()


def _assert_published_layers(
    database_url: str,
    version: str,
    *,
    metric_count: int,
    risk_count: int,
) -> None:
    engine = create_engine(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            persisted_metric_count = int(
                session.scalar(select(func.count(MetricValue.id))) or 0
            )
            persisted_risk_count = int(
                session.scalar(select(func.count(RiskResult.id))) or 0
            )
            from app.stocks.derived.workbench_cards import CARD_IDS
            assert metric_count == 14
            assert persisted_metric_count == metric_count + len(CARD_IDS)
            assert persisted_risk_count == risk_count == 12

            metric_rows = session.execute(
                select(MetricValue.metric_id, MetricValue.value).where(
                    MetricValue.ts_code == SYMBOL,
                    MetricValue.as_of == AS_OF,
                    MetricValue.metric_id.in_(EXPECTED_METRICS),
                )
            ).all()
            assert dict(metric_rows) == EXPECTED_METRICS

            risk_rows = session.execute(
                select(RiskResult.risk_id, RiskResult.status).where(
                    RiskResult.ts_code == SYMBOL,
                    RiskResult.as_of == AS_OF,
                )
            ).all()
            assert dict(risk_rows) == EXPECTED_RISKS

            page_snapshots = session.scalars(select(PageSnapshot)).all()
            assert {
                (row.product, row.snapshot_version) for row in page_snapshots
            } == {(product, version) for product in EXPECTED_PRODUCTS}
            published = session.scalars(select(PublishedSnapshot)).all()
            assert {row.product for row in published} == EXPECTED_PRODUCTS
            assert {row.version for row in published} == {version}
            completed_builds = session.scalar(
                select(func.count(JobRun.id)).where(
                    JobRun.job_name == "build-and-publish",
                    JobRun.status == "completed",
                )
            )
            assert completed_builds == 1
    finally:
        engine.dispose()


def _expected_tushare_requests() -> list[
    tuple[str, dict[str, object], tuple[str, ...]]
]:
    return [
        (
            "stock_basic",
            {"list_status": "L", "limit": "2", "offset": "0"},
            EXPECTED_FIELDS_BY_API["stock_basic"],
        ),
        (
            "daily",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["daily"],
        ),
        (
            "daily",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "2",
            },
            EXPECTED_FIELDS_BY_API["daily"],
        ),
        (
            "daily_basic",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["daily_basic"],
        ),
        (
            "adj_factor",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["adj_factor"],
        ),
        (
            "adj_factor",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "2",
            },
            EXPECTED_FIELDS_BY_API["adj_factor"],
        ),
        (
            "income",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["income"],
        ),
        (
            "income",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "2",
            },
            EXPECTED_FIELDS_BY_API["income"],
        ),
        (
            "balancesheet",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["balancesheet"],
        ),
        (
            "cashflow",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["cashflow"],
        ),
        (
            "cashflow",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "2",
            },
            EXPECTED_FIELDS_BY_API["cashflow"],
        ),
        (
            "fina_indicator",
            {
                "ts_code": SYMBOL,
                "start_date": "20230824",
                "end_date": "20260824",
                "limit": "2",
                "offset": "0",
            },
            EXPECTED_FIELDS_BY_API["fina_indicator"],
        ),
    ]


def _screener_request() -> dict[str, object]:
    return {
        "filters": {
            "preset": None,
            "enabledThresholds": [],
            "thresholds": {
                "pe": 100,
                "pb": 20,
                "pePercentile": 100,
                "pbPercentile": 100,
                "dividendYield": 0,
                "expectedDividendYield": 0,
                "dividendYears": 0,
                "payoutRatio": 0,
                "capitalReturnRatio": 0,
                "roe": 0,
                "roic": 0,
                "realMoneyIndex": 0,
                "institutionHolding": 0,
                "marketCap": 0,
            },
            "exclusions": [],
            "boards": ["sh-main"],
            "categories": {
                "majorIndustry": [],
                "subIndustry": [],
                "concept": [],
                "region": [],
            },
        },
        "sortRules": [
            {"field": "dividendYield", "direction": "desc", "enabled": True},
        ],
        "page": 1,
        "pageSize": 200,
    }


def _request_database_apis(
    monkeypatch: pytest.MonkeyPatch,
    main_module: ModuleType,
    database_url: str,
    version: str,
) -> None:
    monkeypatch.setattr(
        main_module,
        "settings",
        config_module.Settings(
            data_backend="database",
            database_url=database_url,
            database_read_url=database_url,
        ),
    )

    with TestClient(main_module.create_app()) as client:
        # Securities-only inputs cannot bypass macro quality with empty snapshots.
        assert client.get("/api/data/macro-transmission").status_code == 503
        assert client.get("/api/data/macro-insights").status_code == 503
        responses = {
            "market-overview": client.get("/api/data/market-overview"),
            "market-explorer": client.get("/api/data/market-explorer"),
            "screener": client.post(
                "/api/data/screener",
                json=_screener_request(),
            ),
            "portfolio": client.get("/api/data/portfolio"),
            f"stock-workbench:{SYMBOL}": client.get(
                f"/api/data/stocks/{SYMBOL}"
            ),
        }

    for product, response in responses.items():
        assert response.status_code == 200, (product, response.text)
        body = response.json()
        assert body["meta"]["version"] == version
        payload = body["data"]
        if product == "screener":
            payload = {
                "bootstrap": payload["bootstrap"],
                "stocks": payload["items"],
            }
        assert validate_product_payload(product, payload) == ()

    screener = responses["screener"].json()["data"]
    assert [item["symbol"] for item in screener["items"]] == [SYMBOL]

    portfolio = responses["portfolio"].json()["data"]
    assert {asset["id"] for asset in portfolio["assets"]} == {
        SYMBOL,
        "CNY",
    }

    workbench = responses[f"stock-workbench:{SYMBOL}"].json()["data"]
    assert workbench["quote"]["price"] == 1500
    assert workbench["dailyChart"]["candles"] == [
        {
            "date": "2026-08-20",
            "open": 1431.3725490196077,
            "high": 1460.7843137254902,
            "low": 1421.5686274509803,
            "close": 1450.9803921568628,
            "volume": 90.0,
        },
        {
            "date": "2026-08-21",
            "open": 1465.4901960784314,
            "high": 1485.2941176470588,
            "low": 1455.5882352941176,
            "close": 1475.3921568627452,
            "volume": 95.0,
        },
        {
            "date": "2026-08-24",
            "open": 1490.0,
            "high": 1510.0,
            "low": 1480.0,
            "close": 1500.0,
            "volume": 100.0,
        },
    ]
    assert workbench["metrics"]
    assert {item["id"]: item["status"] for group in workbench["risks"]["groups"]
            for item in group["items"]} == EXPECTED_RISKS
    assert workbench["dailyChart"]["announcements"] == []
    financial_tables = {
        table["kind"]: table
        for table in workbench["financial"]["statements"]["tables"]
    }
    assert set(financial_tables) == {"income", "balance", "cashflow"}
    assert financial_tables["income"]["periods"] == [
        "2024-09-30",
        "2024-12-31",
        "2025-09-30",
    ]
    income_rows = {
        row["label"]: row["values"]
        for row in financial_tables["income"]["rows"]
    }
    assert income_rows["revenue"] == [200.0, 1000.0, 300.0]
    assert income_rows["n_income_attr_p"] == [20.0, 100.0, 30.0]
    balance_rows = {
        row["label"]: row["values"]
        for row in financial_tables["balance"]["rows"]
    }
    assert financial_tables["balance"]["periods"] == ["2025-12-31"]
    assert balance_rows["total_assets"] == [1000.0]
    assert balance_rows["total_liab"] == [300.0]
    assert balance_rows["money_cap"] == [200.0]
    cashflow_rows = {
        row["label"]: row["values"]
        for row in financial_tables["cashflow"]["rows"]
    }
    assert financial_tables["cashflow"]["periods"] == [
        "2024-09-30",
        "2024-12-31",
        "2025-09-30",
    ]
    assert cashflow_rows["n_cashflow_act"] == [20.0, 100.0, 30.0]

    market_overview = responses["market-overview"].json()["data"]
    market_explorer = responses["market-explorer"].json()["data"]
    market_missing = [
        {"source": source, "check": "external-data-coverage"}
        for source in ("commodities", "etfs", "fundFlow", "indices", "core.classification_member", "core.top_holder")
    ] + [
        {"source": source, "reason": "尚未采集真实来源"}
        for source in ("commodities", "etfs", "fundFlow", "indices")
    ] + [
        {"source": series_id, "check": "external-data-coverage"}
        for series_id in (
            "cn.gdp.real_yoy", "cn.cpi.yoy", "cn.cpi.core_yoy", "cn.ppi.yoy",
            "cn.pmi.manufacturing", "cn.industry.value_added_yoy", "cn.money.m1_yoy",
            "cn.money.m2_yoy", "cn.credit.tsf_stock_yoy", "cn.rate.dr007",
            "cn.rate.govbond_10y", "cn.fx.usdcny", "us.cpi.yoy", "us.cpi.core_yoy",
            "us.gdp.real_qoq_annualized", "us.labor.unemployment_rate",
            "us.labor.nonfarm_payroll_change", "us.rate.effective_fed_funds",
            "us.rate.treasury_10y", "us.pce.core_yoy",
        )
    ]
    market_missing.extend({"source": check, "check": check} for check in (
        "source-calendar-coverage", "screener-field-coverage"))
    expected_missing = {json.dumps(row, sort_keys=True, ensure_ascii=False) for row in market_missing}
    assert {json.dumps(row, sort_keys=True, ensure_ascii=False) for row in market_overview["missingData"]} == expected_missing
    assert {json.dumps(row, sort_keys=True, ensure_ascii=False) for row in market_explorer["missingData"]} == expected_missing

    assert market_overview["globalIndices"] == []
    assert market_overview["etfQuotes"] == []
    assert market_overview["fundFlowSnapshot"] == {
        "status": "缺失",
        "northbound": "--",
        "mainForce": "--",
    }
    assert market_explorer["commodityCategories"] == []
    assert market_explorer["commodityData"] == {}
    assert market_explorer["commodityPeriods"] == []
    assert market_explorer["marketNews"] == []
