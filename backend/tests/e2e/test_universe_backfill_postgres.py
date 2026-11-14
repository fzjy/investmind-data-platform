from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
import sys

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

import app.cli.main as cli_module
import app.config as config_module
import app.stocks.jobs.universe_backfill_runtime as runtime_module
from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.strategies import (
    FIELD_REGISTRY,
    STANDARD_API_NAMES,
    Dataset,
)
from app.db.models import (
    AdjustmentFactor,
    BalanceSheet,
    BusinessSegment,
    CashflowStatement,
    DailyBasic,
    DailyPrice,
    Dividend,
    FinancialExpress,
    FinancialForecast,
    FinancialIndicator,
    IncomeStatement,
    RejectedRecord,
    Repurchase,
    Security,
    ShareUnlock,
    ShareholderCount,
    SourcePayload,
    TopHolder,
)
from app.jobs.build_and_publish import BuildAndPublishReport
from app.jobs.locks import PostgresAdvisoryJobLock
from app.stocks.jobs.universe_backfill import (
    UniverseBackfillCounts,
    UniverseBackfillReport,
)
from app.stocks.jobs.universe_backfill_history import (
    JOB_NAME,
    SqlUniverseBackfillHistory,
)
from app.stocks.jobs.universe_backfill_runtime import open_universe_backfill_runtime
from app.marts.contracts import QualityResult


PROJECT_ROOT = Path(__file__).resolve().parents[3]
POSTGRES_IMAGE = "postgres:17-alpine"
TUSHARE_ENDPOINT = "https://api.tushare.pro"
TEST_TOKEN = "disposable-universe-test-token"
AS_OF = date(2026, 9, 12)
SYMBOLS = ("000001.SZ", "000002.SZ", "000003.SZ")
EXTERNAL_SYMBOL = "300999.SZ"
MALFORMED_DATE = "controlled-invalid-date"

RUN_POSTGRES = pytest.mark.skipif(
    os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
    reason="set DANG_RUN_POSTGRES_TESTS=1 to run disposable PostgreSQL tests",
)

API_TO_DATASET = {
    api_name: dataset for dataset, api_name in STANDARD_API_NAMES.items()
}
STOCK_MODELS = (
    DailyPrice,
    DailyBasic,
    AdjustmentFactor,
    IncomeStatement,
    BalanceSheet,
    CashflowStatement,
    FinancialIndicator,
    FinancialForecast,
    FinancialExpress,
    Dividend,
    Repurchase,
    ShareUnlock,
    ShareholderCount,
    TopHolder,
    BusinessSegment,
)
EXPECTED_PRODUCT_KINDS = {
    "market-overview",
    "market-explorer",
    "screener",
    "portfolio",
    "stock-workbench",
}


@dataclass(frozen=True)
class ControlledRequest:
    api_name: str
    params: dict[str, object]
    offset: int
    url: str
    provider_count: int
    filtered_count: int
    failed: bool


@dataclass(frozen=True)
class SnapshotView:
    product: str
    version: str
    payload: dict[str, object]


def _row_for(api_name: str, symbol: str) -> dict[str, object]:
    dataset = API_TO_DATASET[api_name]
    values: dict[str, object] = {
        "ts_code": symbol,
        "symbol": symbol.split(".", 1)[0],
        "name": f"受控股票{symbol[:6]}",
        "exchange": "SSE" if symbol.endswith(".SH") else "SZSE",
        "list_date": "20000101",
        "market": "主板",
        "list_status": "L",
        "cal_date": AS_OF.strftime("%Y%m%d"),
        "is_open": 1,
        "pretrade_date": "20260911",
        "trade_date": AS_OF.strftime("%Y%m%d"),
        "ann_date": "20260220",
        "f_ann_date": "20260220",
        "end_date": "20251231",
        "record_date": "20260601",
        "ex_date": "20260602",
        "pay_date": "20260603",
        "exp_date": "20260910",
        "float_date": "20260701",
        "unlock_date": "20260701",
        "report_type": "1",
        "comp_type": "1",
        "update_flag": "1",
        "type": "预增",
        "proc": "完成",
        "div_proc": "实施",
        "holder_name": "受控股东",
        "holder_type": "机构",
        "segment_name": "受控业务",
        "segment_type": "产品",
        "industry": "受控行业",
        "level": "L1",
        "index_code": "801010.SI",
        "industry_name": "受控行业",
        "parent_code": "0",
        "industry_code": "801010",
        "is_pub": "1",
        "src": "SW2021",
        "open": "10",
        "high": "11",
        "low": "9",
        "close": "10.5",
        "pre_close": "10",
        "change": "0.5",
        "pct_chg": "5",
        "vol": "100",
        "amount": "1000",
        "turnover_rate": "1.5",
        "pe": "10",
        "pb": "1.2",
        "ps": "2",
        "total_mv": "100000",
        "circ_mv": "80000",
        "dv_ratio": "2",
        "dv_ttm": "2.5",
        "total_share": "1000",
        "float_share": "10",
        "free_share": "700",
        "pe_ttm": "11",
        "ps_ttm": "2.1",
        "limit_status": "0",
        "adj_factor": "1.01",
        "n_income_attr_p": "100",
        "oper_cost": "400",
        "revenue": "1000",
        "ebit": "130",
        "total_revenue": "1000",
        "total_profit": "125",
        "income_tax": "25",
        "n_income": "100",
        "fin_exp_int_exp": "5",
        "total_assets": "2000",
        "total_liab": "600",
        "total_hldr_eqy_exc_min_int": "1400",
        "money_cap": "300",
        "inventories": "100",
        "goodwill": "20",
        "st_borr": "50",
        "lt_borr": "100",
        "st_bonds_payable": "0",
        "non_cur_liab_due_1y": "0",
        "bond_payable": "0",
        "lease_liab": "0",
        "n_cashflow_act": "120",
        "c_pay_acq_const_fiolta": "20",
        "n_cashflow_inv_act": "-30",
        "n_cash_flows_fnc_act": "-40",
        "c_recp_cap_contrib": "0",
        "free_cashflow": "100",
        "c_pay_dist_dpcp_int_exp": "30",
        "c_cash_equ_end_period": "300",
        "roe": "20",
        "roic": "18",
        "grossprofit_margin": "60",
        "netprofit_margin": "10",
        "or_yoy": "12",
        "netprofit_yoy": "15",
        "fcff": "80",
        "interestdebt": "150",
        "netdebt": "100",
        "invest_capital": "500",
        "tax_to_ebt": "0.2",
        "debt_to_eqt": "0.43",
        "ocf_to_or": "0.3",
        "p_change_min": "10",
        "p_change_max": "20",
        "net_profit_min": "90",
        "net_profit_max": "110",
        "cash_div": "1",
        "cash_div_tax": "0.8",
        "holder_num": "10000",
        "hold_amount": "500",
    }
    return {field: values.get(field) for field in FIELD_REGISTRY[dataset]}


class ControlledUniverseTushare:
    def __init__(self) -> None:
        self.requests: list[ControlledRequest] = []
        self.fail_on: tuple[str, str, int] | None = None
        self.inject_rejection = False
        self.include_external = True
        self.client_count = 0

    def begin_run(self, fail_on: tuple[str, str, int] | None) -> None:
        self.fail_on = fail_on
        self.inject_rejection = fail_on is not None

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == TUSHARE_ENDPOINT
        payload: dict[str, object] = json.loads(request.content)
        assert set(payload) == {"api_name", "token", "params", "fields"}
        assert payload["token"] == TEST_TOKEN

        api_name = str(payload["api_name"])
        assert api_name in API_TO_DATASET
        dataset = API_TO_DATASET[api_name]
        assert tuple(str(payload["fields"]).split(",")) == FIELD_REGISTRY[dataset]
        params = payload["params"]
        assert isinstance(params, dict)
        assert params["limit"] == "1"
        offset = int(params["offset"])
        symbol = str(params.get("ts_code", ""))

        if self.fail_on == (api_name, symbol, offset):
            self.requests.append(
                ControlledRequest(
                    api_name,
                    dict(params),
                    offset,
                    str(request.url),
                    0,
                    0,
                    True,
                )
            )
            return httpx.Response(
                503,
                request=request,
                text="controlled disposable provider failure",
            )

        rows = self._rows(api_name, params, offset)
        filtered_count = sum(
            row.get("ts_code") == EXTERNAL_SYMBOL for row in rows
        )
        self.requests.append(
            ControlledRequest(
                api_name,
                dict(params),
                offset,
                str(request.url),
                len(rows),
                filtered_count,
                False,
            )
        )
        fields = FIELD_REGISTRY[dataset]
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": list(fields),
                    "items": [[row[field] for field in fields] for row in rows],
                },
            },
        )

    def _rows(
        self,
        api_name: str,
        params: dict[str, object],
        offset: int,
    ) -> tuple[dict[str, object], ...]:
        dataset = API_TO_DATASET[api_name]
        if dataset is Dataset.SECURITY:
            rows = tuple(_row_for(api_name, symbol) for symbol in SYMBOLS)
            return rows[offset : offset + 1]
        if dataset is Dataset.TRADE_CALENDAR:
            return (_row_for(api_name, SYMBOLS[0]),) if offset == 0 else ()
        if dataset is Dataset.CLASSIFICATION:
            row = _row_for(api_name, "801010.SI")
            row["name"] = "受控行业"
            return (row,) if offset == 0 else ()

        symbol = str(params["ts_code"])
        if (
            self.include_external
            and dataset is Dataset.DAILY
            and symbol == SYMBOLS[0]
        ):
            external = _row_for(api_name, EXTERNAL_SYMBOL)
            external["trade_date"] = "20260911"
            selected = _row_for(api_name, symbol)
            return (external, selected)[offset : offset + 1]
        if dataset is Dataset.DAILY and symbol == SYMBOLS[2]:
            prior = _row_for(api_name, symbol)
            prior["trade_date"] = "20260911"
            current = _row_for(api_name, symbol)
            return (prior, current)[offset : offset + 1]
        if (
            self.inject_rejection
            and dataset is Dataset.ADJ_FACTOR
            and symbol == SYMBOLS[2]
            and offset == 0
        ):
            malformed = _row_for(api_name, symbol)
            malformed["trade_date"] = MALFORMED_DATE
            return (malformed,)
        return (_row_for(api_name, symbol),) if offset == 0 else ()


class UniverseBackfillPostgresHarness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path
        self._stack = ExitStack()
        self.source = ControlledUniverseTushare()
        self.database_url = ""
        self.engine: Engine | None = None
        self.run_ranges: list[tuple[int, int]] = []

    def __enter__(self) -> UniverseBackfillPostgresHarness:
        try:
            postgres = self._stack.enter_context(
                PostgresContainer(POSTGRES_IMAGE, driver="psycopg")
            )
            self.database_url = postgres.get_connection_url(driver="psycopg")
            self._configure_environment()
            command.upgrade(
                Config(str(PROJECT_ROOT / "backend" / "alembic.ini")),
                "head",
            )
            self.engine = create_engine(self.database_url)
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *_error: object) -> None:
        try:
            if self.engine is not None:
                self.engine.dispose()
        finally:
            self._stack.close()

    def _configure_environment(self) -> None:
        universe_file = self._tmp_path / "controlled-universe.json"
        universe_file.write_text(json.dumps(SYMBOLS), encoding="utf-8")
        self._monkeypatch.setattr(
            config_module,
            "PROJECT_ENV_FILE",
            self._tmp_path / "missing-project.env",
        )
        self._monkeypatch.setenv("DANG_DATA_BACKEND", "database")
        self._monkeypatch.setenv("DANG_DATABASE_URL", self.database_url)
        self._monkeypatch.setenv("DANG_DATABASE_READ_URL", self.database_url)
        self._monkeypatch.setenv("DANG_TUSHARE_TOKEN", TEST_TOKEN)
        self._monkeypatch.setenv("DANG_TUSHARE_MODE", "standard")
        self._monkeypatch.setenv("DANG_TUSHARE_PAGE_SIZE", "1")
        self._monkeypatch.setenv(
            "DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE",
            "60000",
        )
        self._monkeypatch.setenv("DANG_SYNC_UNIVERSE", "custom")
        self._monkeypatch.setenv("DANG_SYNC_UNIVERSE_FILE", str(universe_file))
        self._monkeypatch.setenv("DANG_BACKFILL_AS_OF", AS_OF.isoformat())
        self._monkeypatch.setenv("DANG_BACKFILL_MARKET_YEARS", "1")
        self._monkeypatch.setenv(
            "DANG_BACKFILL_FINANCIAL_START",
            "2025-01-01",
        )
        self._monkeypatch.setenv("DANG_BACKFILL_BATCH_SIZE", "2")
        self._monkeypatch.chdir(PROJECT_ROOT)

        def controlled_client(token: str, **_kwargs: object) -> TushareClient:
            assert token == TEST_TOKEN
            self.source.client_count += 1
            return TushareClient(
                token,
                transport=httpx.MockTransport(self.source.handle),
                max_retries=0,
            )

        def deny_real_http(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("real HTTP transport is forbidden in this test")

        self._monkeypatch.setattr(runtime_module, "TushareClient", controlled_client)
        self._monkeypatch.setattr(
            httpx.HTTPTransport,
            "handle_request",
            deny_real_http,
        )

    def run(
        self,
        *,
        fail_on: tuple[str, str, int] | None = None,
        page_size: int = 1,
        force: bool = False,
    ) -> UniverseBackfillReport:
        assert page_size == 1, "the controlled source is intentionally page-sized"
        self._monkeypatch.setenv("DANG_TUSHARE_PAGE_SIZE", str(page_size))
        self.source.begin_run(fail_on)
        start = len(self.source.requests)
        try:
            with open_universe_backfill_runtime(Settings.from_env()) as runtime:
                assert runtime.selection.symbols == SYMBOLS
                assert runtime.selection.profile == "custom"
                assert runtime.selection.coverage == "custom"
                report = runtime.job.run(replace(runtime.options, force=force))
        finally:
            self.source.fail_on = None
            self.source.inject_rejection = False
        self.run_ranges.append((start, len(self.source.requests)))
        return report

    def select_all_universe(self) -> None:
        self._monkeypatch.setenv("DANG_SYNC_UNIVERSE", "all")
        self._monkeypatch.delenv("DANG_SYNC_UNIVERSE_FILE", raising=False)
        self.source.include_external = False

    def seed_active_securities(self) -> None:
        assert self.engine is not None
        with self.engine.begin() as connection:
            for symbol in SYMBOLS:
                connection.execute(
                    text(
                        "INSERT INTO core.security "
                        "(ts_code, symbol, name, exchange, status, is_st, data, source) "
                        "VALUES (:ts_code, :symbol, :name, :exchange, 'L', false, "
                        "CAST('{}' AS jsonb), 'controlled-setup')"
                    ),
                    {
                        "ts_code": symbol,
                        "symbol": symbol.split(".", 1)[0],
                        "name": f"受控股票{symbol[:6]}",
                        "exchange": "SSE" if symbol.endswith(".SH") else "SZSE",
                    },
                )

    def run_cli(self, *, confirm_full_market: bool) -> object:
        self.source.begin_run(None)
        start = len(self.source.requests)
        clients_before = self.source.client_count
        args = ["backfill-universe"]
        if confirm_full_market:
            args.append("--confirm-full-market")
        result = CliRunner().invoke(cli_module.app, args)
        if self.source.client_count > clients_before:
            self.run_ranges.append((start, len(self.source.requests)))
        return result

    def business_state(self) -> tuple[object, ...]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            tables = connection.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_schema IN ('raw', 'core', 'ops', 'mart') "
                    "AND table_type = 'BASE TABLE' "
                    "ORDER BY table_schema, table_name"
                )
            ).all()
            counts = tuple(
                (
                    str(schema),
                    str(table_name),
                    int(
                        connection.scalar(
                            text(f'SELECT count(*) FROM "{schema}"."{table_name}"')
                        )
                        or 0
                    ),
                )
                for schema, table_name in tables
            )
            securities = tuple(
                connection.execute(
                    text(
                        "SELECT ts_code, symbol, name, exchange, status, is_st, "
                        "data, source FROM core.security ORDER BY ts_code"
                    )
                ).all()
            )
        return counts, securities

    def migration_facts(self) -> tuple[int, str]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            major = connection.scalar(
                text("SELECT current_setting('server_version_num')::int / 10000")
            )
            head = connection.scalar(text("SELECT version_num FROM alembic_version"))
        return int(major), str(head)

    def latest_run_details(self) -> dict[str, object]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            details = connection.scalar(
                text(
                    "SELECT details FROM ops.job_run "
                    "WHERE job_name = :job_name "
                    "ORDER BY started_at DESC, id DESC LIMIT 1"
                ),
                {"job_name": JOB_NAME},
            )
        assert isinstance(details, dict)
        return deepcopy(details)

    def run_details(self, run_id: str | None) -> dict[str, object]:
        assert self.engine is not None
        assert run_id is not None
        with self.engine.connect() as connection:
            details = connection.scalar(
                text("SELECT details FROM ops.job_run WHERE id = :run_id"),
                {"run_id": run_id},
            )
        assert isinstance(details, dict)
        return deepcopy(details)

    def cursor_for(self, cursor_key: str) -> dict[str, object]:
        assert self.engine is not None
        job_name = str(self.latest_run_details()["cursorJobName"])
        with self.engine.connect() as connection:
            value = connection.scalar(
                text(
                    "SELECT cursor_value FROM ops.job_cursor "
                    "WHERE job_name = :job_name AND cursor_key = :cursor_key"
                ),
                {"job_name": job_name, "cursor_key": cursor_key},
            )
        assert isinstance(value, dict)
        return deepcopy(value)

    def published_versions(self) -> tuple[str, ...]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            versions = connection.scalars(
                text("SELECT DISTINCT version FROM mart.published_snapshot ORDER BY version")
            ).all()
        return tuple(str(version) for version in versions)

    def job_run_count(self, job_name: str) -> int:
        assert self.engine is not None
        with self.engine.connect() as connection:
            count = connection.scalar(
                text("SELECT count(*) FROM ops.job_run WHERE job_name = :job_name"),
                {"job_name": job_name},
            )
        return int(count or 0)

    def published_snapshots(self) -> tuple[SnapshotView, ...]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT product, version, payload "
                    "FROM mart.published_snapshot ORDER BY product"
                )
            ).mappings()
            return tuple(
                SnapshotView(
                    product=str(row["product"]),
                    version=str(row["version"]),
                    payload=deepcopy(row["payload"]),
                )
                for row in rows
            )

    def open_cursors(
        self,
        cursor_job_name: str | None = None,
    ) -> tuple[tuple[str, str], ...]:
        assert self.engine is not None
        predicate = (
            "job_name = :job_name"
            if cursor_job_name
            else "job_name LIKE :job_name"
        )
        job_name = cursor_job_name or f"{JOB_NAME}:%"
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT job_name, cursor_key FROM ops.job_cursor "
                    f"WHERE {predicate} ORDER BY job_name, cursor_key"
                ),
                {"job_name": job_name},
            ).all()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def first_batch_request_count_after_resume(self) -> int:
        assert len(self.run_ranges) >= 2
        start, end = self.run_ranges[1]
        return sum(
            request.params.get("ts_code") in SYMBOLS[:2]
            for request in self.source.requests[start:end]
        )

    def database_counts(self) -> dict[str, int]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            batches = connection.execute(
                text(
                    "SELECT request_count, row_count, rejected_count "
                    "FROM raw.ingestion_batch"
                )
            ).all()
            raw_items = connection.scalars(
                text("SELECT payload FROM raw.source_payload")
            ).all()
        return {
            "requestCount": sum(int(row[0]) for row in batches),
            "rowCount": sum(int(row[1]) for row in batches),
            "rejectedCount": sum(int(row[2]) for row in batches),
            "retainedCount": sum(
                len(payload["items"])
                for payload in raw_items
                if isinstance(payload, dict)
            ),
        }

    def mutable_backfill_state(self) -> tuple[object, ...]:
        assert self.engine is not None
        with self.engine.connect() as connection:
            runs = tuple(
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    deepcopy(row[5]),
                )
                for row in connection.execute(
                    text(
                        "SELECT id, status, request_count, row_count, error_message, details "
                        "FROM ops.job_run WHERE job_name = :job_name ORDER BY id"
                    ),
                    {"job_name": JOB_NAME},
                )
            )
            cursors = tuple(
                (row[0], row[1], deepcopy(row[2]), row[3])
                for row in connection.execute(
                    text(
                        "SELECT job_name, cursor_key, cursor_value, updated_at "
                        "FROM ops.job_cursor WHERE job_name LIKE :prefix "
                        "ORDER BY job_name, cursor_key"
                    ),
                    {"prefix": f"{JOB_NAME}:%"},
                )
            )
            snapshots = tuple(
                (
                    row[0],
                    row[1],
                    row[2],
                    deepcopy(row[3]),
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                )
                for row in connection.execute(
                    text(
                        "SELECT product, version, as_of, payload, stale, stale_reason, "
                        "freshness_checked_at, published_at "
                        "FROM mart.published_snapshot ORDER BY product"
                    )
                )
            )
        return runs, cursors, snapshots

    def assert_raw_core_and_rejections(self) -> None:
        assert self.engine is not None
        with self.engine.connect() as connection:
            raw_payloads = connection.execute(
                select(SourcePayload.api_name, SourcePayload.payload)
            ).all()
            rejected = connection.scalars(select(RejectedRecord.raw_record)).all()
            securities = set(connection.scalars(select(Security.ts_code)))
            assert securities == set(SYMBOLS)
            for model in STOCK_MODELS:
                assert set(connection.scalars(select(model.ts_code))) == set(
                    SYMBOLS
                )

        raw_symbols_by_api: dict[str, set[str]] = {}
        for api_name, payload in raw_payloads:
            assert isinstance(payload, dict)
            fields = payload["fields"]
            items = payload["items"]
            assert isinstance(fields, list)
            assert isinstance(items, list)
            if "ts_code" not in fields:
                continue
            symbol_index = fields.index("ts_code")
            raw_symbols_by_api.setdefault(str(api_name), set()).update(
                str(item[symbol_index]) for item in items
            )
        assert raw_symbols_by_api["daily"] == set(SYMBOLS)
        assert EXTERNAL_SYMBOL not in json.dumps(
            [payload for _api_name, payload in raw_payloads],
            ensure_ascii=False,
        )
        assert any(
            isinstance(row, dict)
            and row.get("ts_code") == SYMBOLS[2]
            and row.get("trade_date") == MALFORMED_DATE
            for row in rejected
        )

    def assert_network_and_scope(self) -> None:
        assert self.source.client_count == len(self.run_ranges)
        assert self.source.requests
        assert {request.url for request in self.source.requests} == {
            TUSHARE_ENDPOINT
        }
        requested_symbols = {
            str(request.params["ts_code"])
            for request in self.source.requests
            if "ts_code" in request.params
        }
        assert requested_symbols == set(SYMBOLS)


def test_harness_enter_closes_started_container_when_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class StartedContainer:
        def __enter__(self) -> "StartedContainer":
            events.append("container.enter")
            return self

        def __exit__(self, *_error: object) -> None:
            events.append("container.exit")

        @staticmethod
        def get_connection_url(*, driver: str) -> str:
            assert driver == "psycopg"
            return "postgresql+psycopg://controlled.invalid/test"

    monkeypatch.setattr(
        sys.modules[__name__],
        "PostgresContainer",
        lambda *_args, **_kwargs: StartedContainer(),
    )
    harness = UniverseBackfillPostgresHarness(monkeypatch, tmp_path)

    def interrupt_setup() -> None:
        raise KeyboardInterrupt("controlled setup interruption")

    monkeypatch.setattr(harness, "_configure_environment", interrupt_setup)

    with pytest.raises(KeyboardInterrupt, match="controlled setup interruption"):
        harness.__enter__()

    assert events == ["container.enter", "container.exit"]


@pytest.fixture
def harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[UniverseBackfillPostgresHarness]:
    with UniverseBackfillPostgresHarness(monkeypatch, tmp_path) as value:
        yield value


@pytest.mark.postgres
@RUN_POSTGRES
def test_universe_backfill_resumes_after_failed_second_batch(
    harness: UniverseBackfillPostgresHarness,
) -> None:
    config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
    assert harness.migration_facts() == (17, ScriptDirectory.from_config(config).get_current_head())

    first = harness.run(fail_on=("daily", SYMBOLS[2], 1), page_size=1)
    assert first.status == "partial"
    assert first.completed_batches == 1
    assert first.plan.symbol_batches == (SYMBOLS[:2], SYMBOLS[2:])
    assert harness.published_versions() == ()
    checkpoint = harness.latest_run_details()
    assert checkpoint["nextBatchIndex"] == 1
    assert harness.cursor_for(f"daily:{SYMBOLS[2]}")["offset"] == 1

    second = harness.run()
    assert second.status == "completed"
    assert second.resumed is True
    assert second.completed_batches == 2
    assert len(harness.published_versions()) == 1
    assert harness.job_run_count("build-and-publish") == 1
    assert harness.first_batch_request_count_after_resume() == 0

    second_start, second_end = harness.run_ranges[1]
    resumed_daily_offsets = [
        request.offset
        for request in harness.source.requests[second_start:second_end]
        if request.api_name == "daily"
        and request.params.get("ts_code") == SYMBOLS[2]
    ]
    assert resumed_daily_offsets == [1, 2]
    assert 0 not in resumed_daily_offsets

    failed_requests = [request for request in harness.source.requests if request.failed]
    assert [
        (request.api_name, request.params.get("ts_code"), request.offset)
        for request in failed_requests
    ] == [("daily", SYMBOLS[2], 1)]

    details = harness.latest_run_details()
    assert details["coverage"] == "custom"
    assert details["universeSize"] == 3
    assert details["counts"] == second.counts.to_payload()
    assert details["counts"] == {
        "requestCount": 128,
        "providerCount": 66,
        "retainedCount": 65,
        "filteredCount": 1,
        "rowCount": 64,
        "rejectedCount": 1,
    }
    database_counts = harness.database_counts()
    assert details["counts"]["rowCount"] == database_counts["rowCount"]
    assert details["counts"]["rejectedCount"] == database_counts["rejectedCount"]
    assert details["counts"]["retainedCount"] == database_counts["retainedCount"]
    assert details["counts"]["retainedCount"] == (
        details["counts"]["rowCount"] + details["counts"]["rejectedCount"]
    )
    assert details["counts"]["providerCount"] == (
        details["counts"]["retainedCount"]
        + details["counts"]["filteredCount"]
    )
    assert details["counts"]["filteredCount"] == sum(
        request.filtered_count for request in harness.source.requests
    )
    assert details["counts"]["providerCount"] == sum(
        request.provider_count
        for request in harness.source.requests
        if not request.failed
    )
    assert details["counts"]["requestCount"] == (
        database_counts["requestCount"]
        + sum(request.failed for request in harness.source.requests)
    )

    filtered_full_page = [
        request
        for request in harness.source.requests
        if request.api_name == "daily"
        and request.params.get("ts_code") == SYMBOLS[0]
    ]
    assert [request.offset for request in filtered_full_page[:3]] == [0, 1, 2]
    assert filtered_full_page[0].provider_count == 1
    assert filtered_full_page[0].filtered_count == 1

    harness.assert_raw_core_and_rejections()
    snapshots = harness.published_snapshots()
    assert len(snapshots) == 4 + len(SYMBOLS)
    assert {row.product.split(":", 1)[0] for row in snapshots} == (
        EXPECTED_PRODUCT_KINDS
    )
    assert {row.version for row in snapshots} == set(harness.published_versions())
    assert all(
        "full-market" not in json.dumps(row.payload, ensure_ascii=False)
        for row in snapshots
    )
    screener = next(row for row in snapshots if row.product == "screener")
    assert {stock["symbol"] for stock in screener.payload["stocks"]} == set(
        SYMBOLS
    )

    before_status = harness.mutable_backfill_state()
    status = CliRunner().invoke(cli_module.app, ["sync-status"])
    assert status.exit_code in {0, 1}, (status.output, status.exception)
    assert json.loads(status.stdout)["status"] in {"ok", "degraded"}
    assert harness.mutable_backfill_state() == before_status
    harness.assert_network_and_scope()


@pytest.mark.postgres
@RUN_POSTGRES
def test_completed_run_skips_and_force_creates_new_namespace(
    harness: UniverseBackfillPostgresHarness,
) -> None:
    first = harness.run()
    assert first.status == "completed"
    first_cursor_job_name = f"{JOB_NAME}:{first.job_run_id}"
    assert harness.run_details(first.job_run_id)["cursorJobName"] == (
        first_cursor_job_name
    )
    assert harness.open_cursors(first_cursor_job_name) == ()

    first_request_count = len(harness.source.requests)
    skipped = harness.run()
    assert skipped.status == "skipped"
    assert skipped.job_run_id == first.job_run_id
    skipped_request_count = len(harness.source.requests)
    assert skipped_request_count == first_request_count
    assert harness.open_cursors(first_cursor_job_name) == ()

    forced = harness.run(force=True)
    assert forced.status == "completed"
    assert forced.job_run_id != first.job_run_id
    forced_cursor_job_name = f"{JOB_NAME}:{forced.job_run_id}"
    assert harness.run_details(forced.job_run_id)["cursorJobName"] == (
        forced_cursor_job_name
    )
    assert harness.open_cursors(forced_cursor_job_name) == ()

    assert harness.job_run_count("build-and-publish") == 2
    assert harness.open_cursors() == ()
    harness.assert_network_and_scope()


@pytest.mark.postgres
@RUN_POSTGRES
def test_top_level_advisory_lock_is_exclusive_across_connections(
    harness: UniverseBackfillPostgresHarness,
) -> None:
    holder_engine = create_engine(harness.database_url)
    holder = PostgresAdvisoryJobLock(holder_engine)
    key = ""
    try:
        with open_universe_backfill_runtime(Settings.from_env()) as runtime:
            key = f"{JOB_NAME}:{runtime.job.plan(runtime.options).business_key}"
            assert holder.try_acquire(key) is True
            locked = runtime.job.run(runtime.options)
            assert locked.status == "locked"
            assert harness.source.requests == []

            holder.release(key)
            completed = runtime.job.run(runtime.options)
            assert completed.status == "completed"
            assert harness.published_versions()
    finally:
        if key:
            holder.release(key)
        holder_engine.dispose()

    harness.run_ranges.append((0, len(harness.source.requests)))
    harness.assert_network_and_scope()


@pytest.mark.postgres
@RUN_POSTGRES
def test_all_cli_requires_confirmation_before_any_network_or_business_write(
    harness: UniverseBackfillPostgresHarness,
) -> None:
    """Three controlled active symbols prove the all path, not a real full market."""
    harness.seed_active_securities()
    harness.select_all_universe()
    before = harness.business_state()

    denied = harness.run_cli(confirm_full_market=False)

    assert denied.exit_code == 2
    assert "requires --confirm-full-market" in denied.stderr
    assert harness.source.client_count == 0
    assert harness.source.requests == []
    assert harness.job_run_count(JOB_NAME) == 0
    assert harness.business_state() == before

    confirmed = harness.run_cli(confirm_full_market=True)

    payload = json.loads(confirmed.stdout)
    assert payload["status"] == "completed", json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )
    assert confirmed.exit_code == 0, (confirmed.output, confirmed.exception)
    assert payload["universe"] == {
        "profile": "all",
        "version": payload["universe"]["version"],
        "coverage": "full-market",
        "size": 3,
    }
    preflight = json.loads(confirmed.stderr.splitlines()[0])
    assert preflight["coverage"] == "full-market"
    assert preflight["universeSize"] == 3
    assert harness.latest_run_details()["coverage"] == "full-market"
    harness.assert_network_and_scope()


@pytest.mark.postgres
@RUN_POSTGRES
def test_postgres_history_preserves_plan_progress_and_terminal_quality(
    harness: UniverseBackfillPostgresHarness,
) -> None:
    with open_universe_backfill_runtime(Settings.from_env()) as runtime:
        plan = runtime.job.plan(runtime.options)
        history = SqlUniverseBackfillHistory(runtime.session_factory)
        decision = history.begin(
            plan,
            datetime(2026, 9, 13, 8, 0, tzinfo=UTC),
            force=False,
        )
        initial = harness.run_details(decision.run_id)
        assert initial["asOf"] == "2026-09-12"
        assert initial["mode"] == "standard"
        assert initial["marketStart"] == "2025-09-12"
        assert initial["financialStart"] == "2025-01-01"
        assert initial["batchSize"] == 2
        assert initial["batchCount"] == 2

        counts = UniverseBackfillCounts(
            provider_count=5,
            retained_count=4,
            filtered_count=1,
            row_count=4,
            request_count=2,
        )
        history.checkpoint(
            decision.run_id,
            next_batch_index=1,
            global_completed=True,
            current_dataset="daily",
            counts=counts,
            now=datetime(2026, 9, 13, 9, 0, tzinfo=UTC),
        )
        checkpoint = harness.run_details(decision.run_id)
        assert checkpoint["asOf"] == initial["asOf"]
        assert checkpoint["mode"] == initial["mode"]
        assert checkpoint["marketStart"] == initial["marketStart"]
        assert checkpoint["financialStart"] == initial["financialStart"]
        assert checkpoint["batchSize"] == initial["batchSize"]
        assert checkpoint["batchCount"] == initial["batchCount"]
        assert checkpoint["nextBatchIndex"] == 1
        assert checkpoint["counts"] == counts.to_payload()

        quality = QualityResult(
            "source-completeness",
            "blocking",
            True,
            {"eligibleSecurityCount": 3},
        )
        report = UniverseBackfillReport(
            status="completed",
            job_run_id=decision.run_id,
            plan=plan,
            resumed=False,
            completed_batches=2,
            counts=counts,
            publish_report=BuildAndPublishReport(
                status="completed",
                job_run_id="publish-run",
                as_of=AS_OF,
                version="controlled-v1",
                symbols=SYMBOLS,
                metric_count=0,
                risk_count=0,
                products=("market-overview",),
                quality=(quality,),
                missing_data=(),
            ),
        )
        history.finish(
            decision.run_id,
            "completed",
            datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
            details=report.to_payload(),
        )

    terminal = harness.run_details(decision.run_id)
    assert terminal["asOf"] == "2026-09-12"
    assert terminal["nextBatchIndex"] == 1
    assert terminal["publish"]["quality"] == [
        {
            "name": "source-completeness",
            "severity": "blocking",
            "passed": True,
            "details": {"eligibleSecurityCount": 3},
        }
    ]
