from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

import app.config as config_module
import app.macro.jobs.macro_sync as sync_module
import app.macro.jobs.macro_sync_runtime as runtime_module
import app.jobs.sync_status as status_module
from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.connectors.external_files.reader import ExternalFileReader
from app.ingestion.external_import import ExternalImporter
from app.db.models import (
    IngestionBatch, JobFailure, JobRun, MacroObservation, MacroSeriesState,
    PageSnapshot, PublishedSnapshot, QualityCheckResult, RejectedRecord, SourcePayload,
)
from app.jobs.locks import PostgresAdvisoryJobLock, macro_sync_lock_key
from app.macro.jobs.macro_scheduler import MacroScheduledCoordinator, most_recent_macro_schedule
from app.macro.jobs.macro_sync import MacroSyncReport
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.jobs.scheduler_state import SchedulerStateRepository
from app.jobs.sync_status import MacroStatusReader
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.marts.contracts import MartCandidate
from app.macro.marts.macro import MacroRepository
from app.marts.publication import SnapshotPublisher
from backend.tests.e2e.controlled_macro import (
    AS_OF, CATALOG_VERSION, FALLBACK_ID, MISSING_ID, REVISION_ID, NONFINITE_ID, STALE_ID,
    TEST_FRED_KEY, TEST_TUSHARE_TOKEN, PERIODS, ControlledMacroResponses, write_controlled_catalog,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PRODUCTS = ("macro-transmission", "macro-insights")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.environ.get("DANG_RUN_POSTGRES_TESTS") != "1",
        reason="set DANG_RUN_POSTGRES_TESTS=1 for disposable PostgreSQL 17 only",
    ),
]


@dataclass
class MacroHarness:
    settings: Settings
    catalog: MacroSeriesCatalog
    source: ControlledMacroResponses
    engine: Engine
    factory: sessionmaker[Session]
    import_root: Path

    def seed_offline(self) -> None:
        # Real import entry/ledger, original source/URL: not fabricated provider coverage.
        stage = "first" if self.source.stage == "first" else "restored"
        records = [{
            "schemaVersion": "1.0", "recordType": "macro", "source": "task11-offline",
            "seriesId": series.id, "name": series.name, "region": series.region,
            "frequency": series.frequency, "unit": series.unit,
            "seasonalAdjustment": series.seasonal_adjustment,
            "observedAt": period.isoformat(), "releasedAt": AS_OF.isoformat(),
            "revision": "controlled-import", "value": f"{index + 1}.1",
            "sourceUrl": f"https://example.com/task11/offline/{series.id}",
        } for series in self.catalog.series if not series.sources
           and (stage != "first" or series.id != MISSING_ID)
           for index, period in enumerate(PERIODS[series.frequency])]
        path = self.import_root / f"offline-{stage}.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        report = ExternalImporter(self.factory, ExternalFileReader(self.import_root)).import_file(path)
        assert report.status in {"imported", "already_imported"}, report

    def runtime(self) -> AbstractContextManager[runtime_module.MacroSyncRuntime]:
        self.seed_offline()
        return runtime_module.open_macro_sync_runtime(
            self.settings, transport=httpx.MockTransport(self.source.handle),
        )

    def run(
        self, *, force: bool = False, as_of: datetime = AS_OF,
        mode: Literal["live", "replay"] = "live",
    ) -> MacroSyncReport:
        with self.runtime() as runtime:
            return runtime.job.run(MacroSyncOptions(
                as_of, 10, runtime.catalog.catalog_version,
                runtime.catalog.catalog_hash, force=force, mode=mode,
            ))

    def pointers(self) -> dict[str, str]:
        with self.factory() as session:
            return dict(session.execute(select(
                PublishedSnapshot.product, PublishedSnapshot.version,
            ).where(PublishedSnapshot.product.in_(PRODUCTS))).all())

    def count(self, model: type[MacroObservation] | type[PageSnapshot]) -> int:
        with self.factory() as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    def status(self) -> dict[str, object]:
        return MacroStatusReader(self.factory, self.settings, catalog=self.catalog).read(self.source.now)


@pytest.fixture
def macro(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MacroHarness]:
    original_catalog = DEFAULT_MACRO_CATALOG_FILE.read_bytes()
    default = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    assert len(default.series) == 20
    assert {series.id for series in default.series if not series.sources} == {
        "cn.credit.tsf_stock_yoy", "cn.fx.usdcny",
    }
    catalog_path = tmp_path / "controlled-only-macro-series.json"
    catalog = write_controlled_catalog(catalog_path)
    source = ControlledMacroResponses(catalog)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return source.now.astimezone(tz) if tz is not None else source.now.replace(tzinfo=None)

    monkeypatch.setattr(sync_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(runtime_module, "DEFAULT_MACRO_CATALOG_FILE", catalog_path)
    monkeypatch.setattr(status_module, "DEFAULT_MACRO_CATALOG_FILE", catalog_path)
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", tmp_path / "missing-project.env")
    # 清除继承部署参数，全部 URL 在 disposable container 启动后赋值。
    for name in tuple(os.environ):
        if name.upper().startswith("DANG_") and name != "DANG_RUN_POSTGRES_TESTS":
            monkeypatch.delenv(name)
    monkeypatch.chdir(PROJECT_ROOT)

    def deny_network(_transport: object, request: httpx.Request) -> httpx.Response:
        source.unexpected_requests.append(str(request.url.copy_with(query=None)))
        raise AssertionError("Task11 禁止真实 HTTP 网络")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)

    def controlled_client(token: str, **kwargs: object) -> TushareClient:
        assert token == TEST_TUSHARE_TOKEN
        rate_limiter = kwargs.get("rate_limiter")
        assert isinstance(rate_limiter, RequestRateLimiter)
        return TushareClient(
            token, transport=httpx.MockTransport(source.handle), max_retries=0,
            rate_limiter=rate_limiter,
        )

    monkeypatch.setattr(runtime_module, "TushareClient", controlled_client)
    with PostgresContainer("postgres:17-alpine", driver="psycopg", password="task11-db-secret-canary") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DANG_DATA_BACKEND", "database")
        monkeypatch.setenv("DANG_DATABASE_URL", database_url)
        monkeypatch.setenv("DANG_DATABASE_READ_URL", database_url)
        monkeypatch.setenv("DANG_FRED_API_KEY", TEST_FRED_KEY)
        monkeypatch.setenv("DANG_TUSHARE_TOKEN", TEST_TUSHARE_TOKEN)
        monkeypatch.setenv("DANG_TUSHARE_STANDARD_REQUESTS_PER_MINUTE", "60000")
        monkeypatch.setenv("DANG_MACRO_SYNC_ENABLED", "true")
        settings = Settings.from_env()
        assert settings.database_url == settings.database_read_url == database_url
        config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
        command.upgrade(config, "head")
        # Alembic fileConfig 默认禁用已存在的 logger；显式打开后证明日志脱敏。
        monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
        monkeypatch.setattr(logging.getLogger("app.connectors.tushare.client"), "disabled", False)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                version = connection.scalar(text("SHOW server_version"))
                assert isinstance(version, str) and version.startswith("17.")
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                assert revision == ScriptDirectory.from_config(config).get_current_head()
                print(f"Task11 PostgreSQL proof: {version}; Alembic head={revision}; disposable={postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}")
            yield MacroHarness(settings, catalog, source, engine, sessionmaker(engine, expire_on_commit=False), tmp_path)
        finally:
            engine.dispose()
    assert DEFAULT_MACRO_CATALOG_FILE.read_bytes() == original_catalog
    assert not source.unexpected_requests


def test_default_nbs_feed_flows_through_real_runtime_audit_core_and_atomic_publication(macro, monkeypatch, tmp_path):
    from backend.tests.connectors.macro.test_nbs_feed import (
        INDEX, CPI_URL, CPI_TITLE, CPI, INDUSTRY_URL, INDUSTRY_TITLE, INDUSTRY, index,
    )
    default = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    targets = {"cn.cpi.core_yoy", "cn.industry.value_added_yoy"}
    payload = macro.catalog.model_dump(mode="json", by_alias=True, exclude={"catalog_hash"})
    payload["catalogVersion"] = "nbs-feed-controlled-v1"
    for item in payload["series"]:
        if item["id"] in targets:
            item["sources"] = [source.model_dump(mode="json", by_alias=True) for source in default.require(item["id"]).sources]
    path = tmp_path / "nbs-feed-catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    macro.catalog = MacroSeriesCatalog.load(path)
    macro.source.stage = "recovery"
    macro.seed_offline()
    monkeypatch.setattr(runtime_module, "DEFAULT_MACRO_CATALOG_FILE", path)
    seen: list[str] = []

    def handle(request):
        url = str(request.url)
        pages = {INDEX: index(CPI_TITLE, CPI_URL) + index(INDUSTRY_TITLE, INDUSTRY_URL), CPI_URL: CPI, INDUSTRY_URL: INDUSTRY}
        if url in pages:
            seen.append(url)
            return httpx.Response(200, text=pages[url])
        return macro.source.handle(request)

    with runtime_module.open_macro_sync_runtime(macro.settings, transport=httpx.MockTransport(handle)) as runtime:
        report = runtime.job.run(MacroSyncOptions(AS_OF, 10, runtime.catalog.catalog_version, runtime.catalog.catalog_hash, mode="live"))
    assert report.status == "partial"  # 最新有效，但受控目录不足以声称十年历史完整。
    assert report.publication_version is not None
    assert seen.count(INDEX) == 2 and set(seen) == {INDEX, CPI_URL, INDUSTRY_URL}
    with macro.factory() as session:
        rows = session.scalars(select(MacroObservation).where(MacroObservation.source == "nbs", MacroObservation.series_id.in_(targets))).all()
        assert {row.series_id: row.value for row in rows} == {"cn.cpi.core_yoy": Decimal("1.0"), "cn.industry.value_added_yoy": Decimal("5.2")}
        audits = session.scalars(select(SourcePayload).join(IngestionBatch).where(IngestionBatch.source == "nbs", IngestionBatch.dataset.in_([f"macro:{key}" for key in targets]))).all()
        assert len(audits) == 2
        assert all(row.payload["data"]["missingWindows"] for row in audits)
        snapshots = session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(PRODUCTS))).all()
        assert {row.version for row in snapshots} == {report.publication_version}
        for snapshot in snapshots:
            selected = {status["id"]: status for status in snapshot.payload["seriesStatus"] if status["id"] in targets}
            assert all(status["actualSource"] == "nbs" and status["stale"] for status in selected.values())
            assert all(status["staleReason"] == "MACRO_HISTORY_GAP" for status in selected.values())


def test_fred_nonterminating_yoy_replay_preserves_storage_precision(macro: MacroHarness) -> None:
    from dataclasses import replace
    from datetime import date

    from app.connectors.macro.contracts import MacroFetchWindow
    from app.connectors.macro.fred import FredMacroProvider
    from app.connectors.macro.transport import MacroHttpTransport
    from app.macro.jobs.macro_sync_repository import MacroObservationRepository

    rows = [
        {"date": "2025-08-01", "value": "300", "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"},
        {"date": "2026-08-01", "value": "310", "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("vintagedates"):
            return httpx.Response(200, json={"vintage_dates": ["2026-09-15"], "count": 1})
        assert request.url.params["units"] == "lin"
        return httpx.Response(200, json={"observations": rows, "count": 2})

    series = macro.catalog.require("us.cpi.yoy")
    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handle)),
                            max_attempts=1, backoff_seconds=(0,)) as transport:
        fetched = FredMacroProvider(TEST_FRED_KEY, transport=transport).fetch(
            series, series.sources[0], MacroFetchWindow(date(2026, 8, 1), AS_OF.date(), AS_OF))
    assert fetched.status == "success" and len(fetched.observations) == 1
    repository = MacroObservationRepository(macro.factory)
    first = repository.persist(series, fetched, AS_OF)
    repeated = repository.persist(series, fetched, AS_OF + timedelta(days=1))
    assert (first.inserted, first.rejected) == (1, 0)
    assert (repeated.unchanged, repeated.rejected) == (1, 0)
    with macro.factory() as session:
        assert session.scalar(select(MacroObservation.value).where(
            MacroObservation.series_id == series.id)) == Decimal("3.3333333333")
    changed = replace(fetched, observations=(replace(fetched.observations[0], value=Decimal("3.3333333334")),))
    conflict = repository.persist(series, changed, AS_OF + timedelta(days=2))
    assert (conflict.inserted, conflict.rejected) == (0, 1)
    with macro.factory() as session:
        assert session.scalar(select(RejectedRecord.reason)) == "PROVIDER_REVISION_VALUE_CONFLICT"


def _api(macro: MacroHarness) -> dict[str, dict[str, object]]:
    import app.main as main_module

    # 标准 create_app + DatabaseProvider + SnapshotRepository + API 路由。
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(main_module, "settings", macro.settings)
        engines: list[Engine] = []
        real_create = main_module.create_engine

        def tracked_create(*args: object, **kwargs: object) -> Engine:
            engine = real_create(*args, **kwargs)
            engines.append(engine)
            return engine

        patch.setattr(main_module, "create_engine", tracked_create)
        try:
            with TestClient(main_module.create_app()) as client:
                assert client.get("/health").json() == {"status": "ok", "database": "ready"}
                results = {}
                for product in PRODUCTS:
                    response = client.get(f"/api/data/{product}")
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    assert payload["meta"]["source"] == "database"
                    assert payload["meta"]["version"] == macro.pointers()[product]
                    assert len(payload["data"]["seriesStatus"]) == 20
                    # 只比较原子发布的快照；collectionAudit 在读取时查询最新采集状态。
                    results[product] = {key: payload[key] for key in ("meta", "data")}
                return results
        finally:
            for engine in engines:
                engine.dispose()


def _series(payload: dict[str, object], series_id: str) -> dict[str, object]:
    return next(row for row in payload["data"]["seriesStatus"] if row["id"] == series_id)


def _assert_secret_free(macro: MacroHarness, *outputs: object) -> None:
    secrets = (TEST_FRED_KEY, TEST_TUSHARE_TOKEN, str(macro.engine.url.password))
    with macro.engine.connect() as connection:
        # 动态检查 ops/raw 的所有文本/JSON 列，包括尚为空的表。
        columns = connection.execute(text("""
            SELECT table_schema, table_name, column_name FROM information_schema.columns
            WHERE table_schema IN ('ops', 'raw')
              AND data_type IN ('text', 'character varying', 'character', 'json', 'jsonb')
            ORDER BY table_schema, table_name, ordinal_position
        """)).all()
        assert columns
        quote = macro.engine.dialect.identifier_preparer.quote
        for schema, table, column in columns:
            values = connection.scalars(text(
                f"SELECT CAST({quote(column)} AS text) FROM {quote(schema)}.{quote(table)}"
            )).all()
            for value in values:
                assert value is None or all(secret not in value for secret in secrets), (schema, table, column)
    rendered = json.dumps(outputs, default=str, ensure_ascii=False)
    assert all(secret not in rendered for secret in secrets)


def _cli_status(macro: MacroHarness) -> dict[str, object]:
    import app.cli.main as cli_module

    statements: list[str] = []
    requests = len(macro.source.requests)

    def deny_provider(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sync-status 不得构造 provider/runtime")

    def capture_sql(
        _connection: object, _cursor: object, statement: str,
        _parameters: object, _context: object, _executemany: bool,
    ) -> None:
        statements.append(statement.strip().upper())

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli_module, "datetime", sync_module.datetime)
        patch.setattr(cli_module, "TushareClient", deny_provider)
        patch.setattr(cli_module, "open_macro_sync_runtime", deny_provider)
        patch.setattr(runtime_module, "TushareClient", deny_provider)
        event.listen(Engine, "before_cursor_execute", capture_sql)
        try:
            result = CliRunner().invoke(cli_module.app, ["sync-status"])
        finally:
            event.remove(Engine, "before_cursor_execute", capture_sql)
    # 六项仅有离线导入，面向采集状态的 CLI 保留 stale。
    assert result.exit_code == 1, (result.output, result.exception)
    payload = json.loads(result.stdout)
    assert payload["status"] == "degraded"
    assert payload["macro"]["series"] == macro.status()["series"]
    assert payload["macro"]["snapshotVersion"] == macro.pointers()["macro-insights"]
    assert payload["macro"]["degraded"] is True
    assert statements and all(sql.startswith(("SELECT", "SHOW")) for sql in statements)
    assert len(macro.source.requests) == requests
    _assert_secret_free(macro, payload, result.output)
    return payload


def test_macro_sync_persists_revisions_and_atomically_publishes_two_products(
    macro: MacroHarness, caplog: pytest.LogCaptureFixture,
) -> None:
    # 捕获缺失回退、丢失 raw/core/state、副本覆盖官方、修订穿越时点和错误 skip。
    caplog.set_level(logging.INFO)
    unrelated = ("market-overview", "screener", "portfolio", "stock-workbench:controlled")
    SnapshotPublisher(macro.factory).publish_many(tuple(
        MartCandidate(product, AS_OF, {"sentinel": product}) for product in unrelated
    ), "unrelated-previous-version", ())
    first = macro.run()
    assert first.status == "partial", first.to_payload()
    assert (first.planned, first.completed, first.fallback, first.missing, first.stale) == (20, 19, 6, 1, 0)
    assert first.row_count == 42  # Five offline imports already persisted separately.
    assert first.request_count == len(macro.source.requests)
    first_request_count = len(macro.source.requests)
    assert macro.pointers() == {product: first.publication_version for product in PRODUCTS}
    first_api = _api(macro)
    for payload in first_api.values():
        assert payload["meta"]["stale"] is True
        assert _series(payload, FALLBACK_ID)["actualSource"] == "tushare"
        assert _series(payload, FALLBACK_ID)["fallbackUsed"] is True
        assert _series(payload, MISSING_ID)["actualSource"] is None
        assert _series(payload, MISSING_ID)["staleReason"] == "missing"
    assert len(first_api["macro-insights"]["data"]["sandboxIndicators"]) == 20
    assert len(first_api["macro-insights"]["data"]["trendDefinitions"]) == 20
    missing_indicator = next(row for row in first_api["macro-insights"]["data"]["sandboxIndicators"] if row["id"] == MISSING_ID)
    assert missing_indicator["values"] == []
    with macro.factory() as session:
        assert set(session.scalars(select(MacroSeriesState.series_id))) == {s.id for s in macro.catalog.series if s.sources}
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 15
        assert set(session.scalars(select(IngestionBatch.dataset))) == {
            f"macro:{s.id}" for s in macro.catalog.series if s.sources
        }
        assert set(session.scalars(select(MacroObservation.series_id))) == {
            s.id for s in macro.catalog.series if s.id != MISSING_ID
        }
        batches = session.scalars(select(IngestionBatch)).all()
        assert all((row.row_count, row.status) == ((0, "failed") if row.source == "nbs" else (3, "completed")) for row in batches)
        assert session.scalar(select(func.count()).select_from(JobFailure)) == 2
    assert all(r.start <= r.end for r in macro.source.requests if r.start is not None)
    assert not any(r.series_id in {s.id for s in macro.catalog.series if not s.sources} for r in macro.source.requests)
    _assert_secret_free(macro, first.to_payload(), first_api, macro.status(), caplog.text)

    # 第二次强制运行使旧值 stale；故障不止写进 report，也进入 state/API。
    macro.source.stage = "second"
    macro.source.now = AS_OF + timedelta(hours=1)
    second = macro.run(force=True)
    assert second.status == "partial", second.to_payload()
    assert (second.attempt, second.stale, second.missing, second.fallback) == (2, 1, 0, 6)
    assert second.publication_version != first.publication_version
    second_api = _api(macro)
    for payload in second_api.values():
        stale = _series(payload, STALE_ID)
        assert (stale["actualSource"], stale["observedAt"], stale["stale"], stale["staleReason"]) == (
            "tushare", "2026-08-01", True, "MACRO_SOURCE_ERROR",
        )
        assert _series(payload, FALLBACK_ID)["actualSource"] == "nbs"
        assert _series(payload, FALLBACK_ID)["fallbackUsed"] is False
        assert _series(payload, MISSING_ID)["actualSource"] == "task11-offline"
    with macro.factory() as session:
        stale_state = session.get(MacroSeriesState, STALE_ID)
        assert stale_state is not None and stale_state.stale
        assert stale_state.consecutive_failures == 1
        assert stale_state.last_success_at == AS_OF
        assert stale_state.last_checked_at == macro.source.now
        revisions = session.scalars(select(MacroObservation).where(
            MacroObservation.series_id == REVISION_ID,
            MacroObservation.observed_at == datetime(2026, 8, 1).date(),
        ).order_by(MacroObservation.released_at)).all()
        assert [(row.revision, row.value) for row in revisions] == [
            ("2026-09-15", Decimal("3.1")), ("2026-09-20", Decimal("3.4")),
        ]
        assert session.scalar(select(func.count()).select_from(MacroObservation).where(
            MacroObservation.series_id == FALLBACK_ID,
            MacroObservation.observed_at == datetime(2026, 8, 1).date(),
        )) == 2
        detected = session.scalars(select(MacroObservation).where(
            MacroObservation.series_id == "cn.money.m1_yoy",
            MacroObservation.observed_at == datetime(2026, 8, 1).date(),
        ).order_by(MacroObservation.released_at)).all()
        assert [row.released_at for row in detected] == [AS_OF, macro.source.now]
        assert [row.value for row in detected] == [Decimal("3.1"), Decimal("3.6")]
        assert detected[0].revision.startswith("initial-import:")
        assert detected[1].revision.startswith("detected:")
    priorities = {s.id: tuple(source.provider for source in s.sources) for s in macro.catalog.series}
    repository = MacroRepository(macro.factory)
    before = repository.visible_observations((REVISION_ID,), datetime(2026, 9, 19, tzinfo=UTC), priorities)
    after = repository.visible_observations((REVISION_ID,), macro.source.now, priorities)
    assert next(row for row in before if row.observed_at.month == 8).value == Decimal("3.1")
    assert next(row for row in after if row.observed_at.month == 8).value == Decimal("3.4")
    selected = repository.visible_observations((FALLBACK_ID,), macro.source.now, priorities)
    assert [(r.observed_at.month, r.source) for r in selected] == [(6, "tushare"), (7, "tushare"), (8, "nbs")]
    assert selected[-1].value == Decimal("3.0")
    api_cpi = next(row for row in second_api["macro-insights"]["data"]["sandboxIndicators"] if row["id"] == REVISION_ID)
    assert api_cpi["values"][-1]["value"] == 3.4
    second_windows = macro.source.requests[first_request_count:]
    assert {r.start.isoformat() for r in second_windows if r.series_id == "us.rate.treasury_10y"} == {"2026-08-21"}
    assert {r.start.isoformat() for r in second_windows if r.series_id == STALE_ID} == {"2025-03-01"}
    assert {r.start.isoformat() for r in second_windows if r.series_id == "cn.gdp.real_yoy"} == {"2023-10-01"}

    count_before_retry = len(macro.source.requests)
    macro.source.stage = "recovery"
    macro.source.now = AS_OF + timedelta(hours=2)
    recovery = macro.run()
    assert recovery.status == "completed", recovery.to_payload()
    assert (recovery.attempt, recovery.completed, recovery.stale, recovery.missing) == (3, 20, 0, 0)
    assert len(macro.source.requests) > count_before_retry
    recovery_api = _api(macro)
    assert all(not payload["meta"]["stale"] and payload["meta"].get("staleReason") is None for payload in recovery_api.values())
    with macro.factory() as session:
        assert all(not state.stale and state.consecutive_failures == 0 for state in session.scalars(select(MacroSeriesState)))
        assert all(row.stale_reason is None for row in session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(PRODUCTS))))
    status = macro.status()
    assert status["series"] == {"total": 20, "completed": 14, "fresh": 14, "fallback": 6, "stale": 6, "missing": 0}
    assert status["openFailures"] == 0 and status["failureSummaries"] == []
    assert status["snapshotVersion"] == recovery.publication_version
    assert status["degraded"] is True

    request_count = len(macro.source.requests)
    core_count = macro.count(MacroObservation)
    pages = macro.count(PageSnapshot)
    skipped = macro.run()
    assert skipped.status == "skipped"
    assert len(macro.source.requests) == request_count
    assert macro.count(MacroObservation) == core_count
    assert macro.count(PageSnapshot) == pages
    forced = macro.run(force=True)
    assert forced.status == "completed" and forced.attempt == 5
    assert forced.row_count == 0
    assert len(macro.source.requests) > request_count
    assert macro.count(MacroObservation) == core_count
    assert macro.pointers() == {product: recovery.publication_version for product in PRODUCTS}
    with macro.factory() as session:
        runs = session.scalars(select(JobRun).where(JobRun.job_name == "sync-macro").order_by(JobRun.started_at)).all()
        assert [run.status for run in runs] == ["partial", "partial", "completed", "skipped", "completed"]
        assert [run.details["attempt"] for run in runs] == [1, 2, 3, 4, 5]
        assert {run.business_key for run in runs} == {f"2026-W38:{CATALOG_VERSION}"}
        assert {run.details["asOf"] for run in runs} == {AS_OF.isoformat()}
        assert {run.details["catalogHash"] for run in runs} == {macro.catalog.catalog_hash}
        assert [(row.product, row.version, row.payload) for row in session.scalars(
            select(PublishedSnapshot).where(PublishedSnapshot.product.in_(unrelated)).order_by(PublishedSnapshot.product)
        )] == [(product, "unrelated-previous-version", {"sentinel": product}) for product in sorted(unrelated)]
        assert set(session.scalars(select(IngestionBatch.dataset))) == {f"macro:{s.id}" for s in macro.catalog.series if s.sources}
        assert all(row.source_url.startswith("https://") for row in session.scalars(select(MacroObservation)))
        payloads = session.scalars(select(SourcePayload)).all()
        assert len({(row.batch_id, row.request_hash) for row in payloads}) == len(payloads)
    assert "HTTP Request: GET https://api.stlouisfed.org" in caplog.text
    assert "api_key=***" in caplog.text
    assert "tushare request completed" in caplog.text
    _cli_status(macro)
    _assert_secret_free(macro, second.to_payload(), recovery.to_payload(), forced.to_payload(), second_api, recovery_api, status, caplog.text)


def test_macro_force_respects_advisory_lock_across_runtime_connections(macro: MacroHarness) -> None:
    # force 不能绕过另一连接的数据库锁，也不能更改已有成功运行和快照。
    macro.source.stage = "recovery"
    completed = macro.run()
    assert completed.status == "completed", completed.to_payload()
    before_api = _api(macro)
    requests = len(macro.source.requests)
    core_count = macro.count(MacroObservation)
    pages = macro.count(PageSnapshot)
    holder = PostgresAdvisoryJobLock(macro.engine)
    key = macro_sync_lock_key(completed.business_key)
    assert holder.try_acquire(key)
    try:
        with macro.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")) >= 1
        locked = macro.run(force=True)
        assert locked.status == "locked"
        assert locked.request_count == 0
        assert len(macro.source.requests) == requests
        assert macro.count(MacroObservation) == core_count
        assert macro.count(PageSnapshot) == pages
        assert _api(macro) == before_api
        with macro.factory() as session:
            previous = session.get(JobRun, completed.job_run_id)
            assert previous is not None and previous.status == "completed"
            assert previous.details["publicationVersion"] == completed.publication_version
    finally:
        holder.release(key)
    retry = macro.run(force=True)
    assert retry.status == "completed"
    assert len(macro.source.requests) > requests
    assert macro.count(MacroObservation) == core_count
    _assert_secret_free(macro, locked.to_payload(), retry.to_payload(), before_api)


def test_macro_nonfinite_response_is_rejected_and_audited_without_secrets(macro: MacroHarness) -> None:
    # NaN 不能进 core；原始响应和拒绝证据应保留，API 仍可读取有限值。
    macro.source.reject_nonfinite = True
    report = macro.run()
    assert report.status == "partial", report.to_payload()
    assert report.rejected_count == 1 and report.row_count == 42
    with macro.factory() as session:
        rejected = session.scalars(select(RejectedRecord)).all()
        assert len(rejected) == 1
        assert rejected[0].reason == "NON_FINITE_VALUE"
        assert rejected[0].raw_record["value"] == "NaN"
        batch = session.get(IngestionBatch, rejected[0].batch_id)
        assert batch is not None and batch.dataset == f"macro:{NONFINITE_ID}"
        assert (batch.status, batch.row_count, batch.rejected_count) == ("partial", 3, 1)
        raw = session.scalar(select(SourcePayload).where(SourcePayload.batch_id == batch.id))
        assert raw is not None
        assert any(row["value"] == "NaN" for row in raw.payload["data"]["observations"])
        assert all(row.value.is_finite() for row in session.scalars(select(MacroObservation)))
    api = _api(macro)
    _assert_secret_free(macro, report.to_payload(), api)


def test_macro_second_snapshot_insert_failure_rolls_back_both_products(macro: MacroHarness) -> None:
    # 数据库 trigger 真正拒绝第二次 PageSnapshot INSERT；序列证明首条 INSERT 已发生。
    first = macro.run()
    assert first.status == "partial"
    old_api = _api(macro)
    pages = macro.count(PageSnapshot)
    core_count = macro.count(MacroObservation)
    with macro.engine.begin() as connection:
        connection.execute(text("CREATE SEQUENCE mart.task11_insert_attempts"))
        connection.execute(text("""
            CREATE FUNCTION mart.task11_fail_second_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM nextval('mart.task11_insert_attempts');
                IF NEW.product = 'macro-insights' THEN
                    RAISE EXCEPTION 'controlled second PageSnapshot insert failure';
                END IF;
                RETURN NEW;
            END $$
        """))
        connection.execute(text("""
            CREATE TRIGGER task11_atomic_failure BEFORE INSERT ON mart.page_snapshot
            FOR EACH ROW EXECUTE FUNCTION mart.task11_fail_second_snapshot()
        """))
    macro.source.stage = "second"
    macro.source.now = AS_OF + timedelta(hours=1)
    try:
        failed = macro.run(force=True)
        assert failed.status == "failed"
        assert failed.error is not None and failed.error.code == "MACRO_PUBLICATION_FAILED"
        assert failed.publication_version is None
        with macro.engine.connect() as connection:
            assert connection.scalar(text("SELECT last_value FROM mart.task11_insert_attempts")) == 2
        assert macro.count(PageSnapshot) == pages
        assert macro.count(MacroObservation) > core_count
        assert macro.pointers() == {product: first.publication_version for product in PRODUCTS}
        assert _api(macro) == old_api
        with macro.factory() as session:
            assert session.scalar(select(func.count()).select_from(QualityCheckResult).where(
                QualityCheckResult.job_run_id == failed.job_run_id,
            )) > 0
    finally:
        with macro.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER task11_atomic_failure ON mart.page_snapshot"))
            connection.execute(text("DROP FUNCTION mart.task11_fail_second_snapshot()"))
            connection.execute(text("DROP SEQUENCE mart.task11_insert_attempts"))
    macro.source.stage = "recovery"
    macro.source.now = AS_OF + timedelta(hours=2)
    recovered = macro.run()
    assert recovered.status == "completed", recovered.to_payload()
    assert macro.pointers() == {product: recovered.publication_version for product in PRODUCTS}
    assert all(not payload["meta"]["stale"] for payload in _api(macro).values())
    _assert_secret_free(macro, failed.to_payload(), recovered.to_payload(), old_api)


def test_macro_quality_block_keeps_current_versions_and_replay_states(macro: MacroHarness) -> None:
    # as-of 不可见的 initial-import 不得用于凑足 80% 覆盖或改写当前 state。
    macro.source.stage = "recovery"
    completed = macro.run()
    assert completed.status == "completed"
    old_api = _api(macro)
    pages = macro.count(PageSnapshot)
    with macro.factory() as session:
        previous_states = [
            tuple(getattr(state, column.name) for column in MacroSeriesState.__table__.columns)
            for state in session.scalars(select(MacroSeriesState).order_by(MacroSeriesState.series_id))
        ]
    blocked = macro.run(force=True, as_of=datetime(2026, 9, 19, 8, tzinfo=UTC), mode="replay")
    assert blocked.status == "failed"
    assert blocked.error is not None and blocked.error.code == "MACRO_PUBLICATION_BLOCKED"
    assert blocked.publication is not None and blocked.publication.blocked
    coverage = next(check for check in blocked.publication.quality if check.name == "macro-coverage-at-least-80")
    # Six monthly/quarterly FRED vintages + one NBS release; daily captured-at
    # and all offline initial imports are later than this replay cutoff.
    assert not coverage.passed and coverage.details == {"available": 7, "total": 20}
    assert macro.count(PageSnapshot) == pages
    assert _api(macro) == old_api
    with macro.factory() as session:
        assert [
            tuple(getattr(state, column.name) for column in MacroSeriesState.__table__.columns)
            for state in session.scalars(select(MacroSeriesState).order_by(MacroSeriesState.series_id))
        ] == previous_states
        saved_quality = session.scalar(select(QualityCheckResult).where(
            QualityCheckResult.job_run_id == blocked.job_run_id,
            QualityCheckResult.check_name == "macro-coverage-at-least-80",
        ))
        assert saved_quality is not None and not saved_quality.passed
    _assert_secret_free(macro, blocked.to_payload(), old_api, macro.status())


def test_macro_startup_only_catches_latest_slot_then_recovers_next_start(macro: MacroHarness) -> None:
    # 多周关机只写最新周的一个 key；partial 每次进程启动最多尝试一次。
    history = SqlMacroSyncHistory(macro.factory)
    state = SchedulerStateRepository(macro.factory)
    old_slot = datetime(2026, 8, 31, 1, tzinfo=UTC)
    old_key = f"2026-W36:{CATALOG_VERSION}"
    old_run = history.start(old_key, old_slot, CATALOG_VERSION, macro.catalog.catalog_hash, "scheduler", 1)
    history.finish(old_run, "completed", {"publicationVersion": "previous-week-success"})
    now = datetime(2026, 9, 22, 3, tzinfo=UTC)
    macro.source.now = now
    assert most_recent_macro_schedule(now, "mon", macro.settings.macro_sync_time, "Asia/Shanghai") == datetime(2026, 9, 21, 1, tzinfo=UTC)

    def coordinator() -> MacroScheduledCoordinator:
        return MacroScheduledCoordinator(
            settings=macro.settings, catalog=macro.catalog, history=history, state=state,
            runtime_factory=macro.runtime, publication_lock=PostgresAdvisoryJobLock(macro.engine),
            clock=lambda: macro.source.now,
        )

    first_process = coordinator()
    partial = first_process.run_startup(now)
    assert partial is not None and partial.status == "partial", partial
    assert partial.business_key == f"2026-W39:{CATALOG_VERSION}"
    assert partial.as_of == datetime(2026, 9, 21, 1, tzinfo=UTC)
    assert partial.publication_as_of == now
    request_count = len(macro.source.requests)
    assert first_process.run_startup(now + timedelta(hours=1)) is None
    assert len(macro.source.requests) == request_count
    alert = state.alert("macro-sync:degraded")
    assert alert is not None and alert.status == "open"
    macro.source.stage = "recovery"
    macro.source.now = now + timedelta(hours=2)
    recovered = coordinator().run_startup(macro.source.now)
    assert recovered is not None and recovered.status == "completed", recovered
    assert recovered.business_key == partial.business_key and recovered.attempt == 2
    assert len(macro.source.requests) > request_count
    assert state.alert("macro-sync:degraded").status == "resolved"
    request_count = len(macro.source.requests)
    assert coordinator().run_startup(macro.source.now) is None
    assert len(macro.source.requests) == request_count
    with macro.factory() as session:
        rows = session.scalars(select(JobRun).where(JobRun.job_name == "sync-macro").order_by(JobRun.started_at)).all()
        assert [row.business_key for row in rows] == [old_key, partial.business_key, partial.business_key]
        assert [row.status for row in rows] == ["completed", "partial", "completed"]
        assert all(row.details["trigger"] == "scheduler" for row in rows)
    assert macro.pointers() == {product: recovered.publication_version for product in PRODUCTS}
    api = _api(macro)
    assert all(not payload["meta"]["stale"] for payload in api.values())
    _assert_secret_free(macro, partial.to_payload(), recovered.to_payload(), api, macro.status())
