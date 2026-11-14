from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

import app.cli.main as cli_module
import app.config as config_module
import app.stocks.jobs.daily_sync_runtime as runtime_module
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.strategies import FIELD_REGISTRY, STANDARD_API_NAMES, Dataset
from app.db.models import (
    AdjustmentFactor,
    BalanceSheet,
    CashflowStatement,
    DailyBasic,
    DailyPrice,
    FinancialIndicator,
    IncomeStatement,
    IngestionBatch,
    JobRun,
    PublishedSnapshot,
    Security,
    SourcePayload,
)
from backend.tests.e2e.controlled_tushare import AS_OF, ROWS_BY_API, SYMBOL, TEST_TOKEN


PROJECT_ROOT = Path(__file__).resolve().parents[3]
TUSHARE_ENDPOINT = "https://api.tushare.pro"
EXCLUDED_SYMBOL = "000001.SZ"

RUN_POSTGRES = pytest.mark.skipif(
    os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
    reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL tests",
)

API_TO_DATASET = {
    api_name: dataset for dataset, api_name in STANDARD_API_NAMES.items()
}


def _excluded(row: dict[str, object]) -> dict[str, object]:
    return {**row, "ts_code": EXCLUDED_SYMBOL}


DAILY_ROWS_BY_API: dict[str, tuple[dict[str, object], ...]] = {
    "stock_basic": (
        dict(ROWS_BY_API["stock_basic"][0]),
        {
            "ts_code": EXCLUDED_SYMBOL,
            "symbol": "000001",
            "name": "平安银行",
            "exchange": "SZSE",
            "list_date": "19910403",
            "market": "主板",
            "list_status": "D",
        },
    ),
    "trade_cal": (
        {
            "exchange": "SSE",
            "cal_date": AS_OF.strftime("%Y%m%d"),
            "is_open": 1,
            "pretrade_date": "20260821",
        },
    ),
    "daily": (
        dict(ROWS_BY_API["daily"][-1]),
        _excluded(dict(ROWS_BY_API["daily"][-1])),
    ),
    "daily_basic": (
        dict(ROWS_BY_API["daily_basic"][0]),
        _excluded(dict(ROWS_BY_API["daily_basic"][0])),
    ),
    "adj_factor": (
        dict(ROWS_BY_API["adj_factor"][-1]),
        _excluded(dict(ROWS_BY_API["adj_factor"][-1])),
    ),
    "income": (
        *tuple(dict(row) for row in ROWS_BY_API["income"]),
        *tuple(_excluded(dict(row)) for row in ROWS_BY_API["income"]),
    ),
    "balancesheet": (
        dict(ROWS_BY_API["balancesheet"][0]),
        _excluded(dict(ROWS_BY_API["balancesheet"][0])),
    ),
    "cashflow": (
        *tuple(dict(row) for row in ROWS_BY_API["cashflow"]),
        *tuple(_excluded(dict(row)) for row in ROWS_BY_API["cashflow"]),
    ),
    "fina_indicator": (
        dict(ROWS_BY_API["fina_indicator"][0]),
        _excluded(dict(ROWS_BY_API["fina_indicator"][0])),
    ),
}


class DailySyncTushare:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == TUSHARE_ENDPOINT
        payload: dict[str, Any] = json.loads(request.content)
        assert payload["token"] == TEST_TOKEN

        api_name = str(payload["api_name"])
        dataset = API_TO_DATASET[api_name]
        fields = tuple(str(payload["fields"]).split(","))
        assert fields == FIELD_REGISTRY[dataset]

        params = payload["params"]
        assert isinstance(params, dict)
        limit = int(params["limit"])
        offset = int(params["offset"])
        self.requests.append((api_name, dict(params)))

        rows = DAILY_ROWS_BY_API.get(api_name, ())[offset : offset + limit]
        for row in rows:
            assert not [field for field in fields if field not in row]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": list(fields),
                    "items": [[row[field] for field in fields] for row in rows],
                },
            },
        )


@pytest.mark.postgres
@RUN_POSTGRES
def test_sync_daily_cli_filters_universe_publishes_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        source = DailySyncTushare()
        universe_file = tmp_path / "custom-universe.json"
        universe_file.write_text(json.dumps([SYMBOL]), encoding="utf-8")
        _configure_environment(monkeypatch, tmp_path, database_url)

        def controlled_client(token: str, **_kwargs: object) -> TushareClient:
            assert token == TEST_TOKEN
            return TushareClient(
                token,
                transport=httpx.MockTransport(source.handle),
                max_retries=0,
            )

        monkeypatch.setattr(runtime_module, "TushareClient", controlled_client)
        args = [
            "sync-daily",
            "--business-date",
            AS_OF.isoformat(),
            "--universe",
            "custom",
            "--universe-file",
            str(universe_file),
            "--symbol",
            SYMBOL,
        ]

        first = CliRunner().invoke(cli_module.app, args)
        first_request_count = len(source.requests)
        second = CliRunner().invoke(cli_module.app, args)
        second_request_count = len(source.requests)
        forced = CliRunner().invoke(cli_module.app, [*args, "--force"])

        assert first.exit_code == 0, (first.output, first.exception)
        assert second.exit_code == 0, (second.output, second.exception)
        assert forced.exit_code == 0, (forced.output, forced.exception)

        first_payload = json.loads(first.stdout)
        second_payload = json.loads(second.stdout)
        forced_payload = json.loads(forced.stdout)
        assert first_payload["status"] == "completed"
        assert first_payload["universe"]["profile"] == "custom"
        assert first_payload["universe"]["size"] == 1
        assert first_payload["providerCount"] > first_payload["retainedCount"]
        assert first_payload["filteredCount"] > 0
        assert first_payload["retainedCount"] + first_payload["filteredCount"] == first_payload["providerCount"]
        assert first_payload["version"]
        assert f"stock-workbench:{SYMBOL}" in first_payload["products"]

        assert second_payload["status"] == "skipped"
        assert second_request_count == first_request_count
        assert len(source.requests) > first_request_count
        assert forced_payload["status"] == "completed"
        assert forced_payload["universe"] == first_payload["universe"]

        _assert_persisted_universe(database_url)

        all_market = CliRunner().invoke(
            cli_module.app,
            [
                "sync-daily",
                "--business-date",
                AS_OF.isoformat(),
                "--universe",
                "all",
                "--confirm-full-market",
                "--symbol",
                SYMBOL,
            ],
        )
        assert all_market.exit_code == 0, (all_market.output, all_market.exception)
        all_market_payload = json.loads(all_market.stdout)
        assert all_market_payload["status"] == "completed"
        assert all_market_payload["universe"]["profile"] == "all"
        _assert_history_scopes(database_url)


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
) -> None:
    monkeypatch.setattr(
        config_module,
        "PROJECT_ENV_FILE",
        tmp_path / "missing-project.env",
    )
    monkeypatch.setenv("DANG_DATA_BACKEND", "database")
    monkeypatch.setenv("DANG_DATABASE_URL", database_url)
    monkeypatch.setenv("DANG_DATABASE_READ_URL", database_url)
    monkeypatch.setenv("DANG_TUSHARE_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DANG_TUSHARE_PAGE_SIZE", "100")
    monkeypatch.setenv("DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE", "60000")
    monkeypatch.setenv("DANG_SYNC_DISCLOSURE_LOOKBACK_DAYS", "1")
    monkeypatch.chdir(PROJECT_ROOT)
    command.upgrade(
        Config(str(PROJECT_ROOT / "backend" / "alembic.ini")),
        "head",
    )


def _assert_persisted_universe(database_url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    stock_models = (
        DailyPrice,
        DailyBasic,
        AdjustmentFactor,
        IncomeStatement,
        BalanceSheet,
        CashflowStatement,
        FinancialIndicator,
    )
    try:
        with factory() as session:
            assert set(session.scalars(select(Security.ts_code))) == {
                SYMBOL,
                EXCLUDED_SYMBOL,
            }
            for model in stock_models:
                assert set(session.scalars(select(model.ts_code))) <= {SYMBOL}

            stock_payloads = session.execute(
                select(SourcePayload.payload)
                .join(IngestionBatch, SourcePayload.batch_id == IngestionBatch.id)
                .where(
                    IngestionBatch.dataset.not_in(
                        ("security", "trade_calendar", "classification")
                    )
                )
            ).scalars()
            assert all(
                EXCLUDED_SYMBOL not in json.dumps(payload, ensure_ascii=False)
                for payload in stock_payloads
            )

            published = session.scalars(select(PublishedSnapshot)).all()
            assert published
            screener = next(row for row in published if row.product == "screener")
            assert {row["symbol"] for row in screener.payload["stocks"]} == {SYMBOL}

    finally:
        engine.dispose()


def _assert_history_scopes(database_url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            daily_runs = session.scalars(
                select(JobRun)
                .where(JobRun.job_name == "sync-daily")
                .order_by(JobRun.started_at)
            ).all()
            assert [run.status for run in daily_runs] == [
                "completed",
                "skipped",
                "completed",
                "completed",
            ]
            assert len({run.business_key for run in daily_runs}) == 2
            assert [run.details["universe"]["profile"] for run in daily_runs] == [
                "custom",
                "custom",
                "custom",
                "all",
            ]
    finally:
        engine.dispose()
