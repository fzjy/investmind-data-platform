"""真实库只读规模报告；显式一次性 PG 合成容量基准，不采集供应商数据。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
import math
import secrets
import time
from typing import Iterator
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.orm import sessionmaker

from app.config import PROJECT_ROOT
from app.db.models import DailyBasic, DailyPrice, Security


class CapacityError(Exception):
    def __init__(self, reason_code: str, exit_code: int = 2):
        self.reason_code = reason_code
        self.exit_code = exit_code
        super().__init__(reason_code)


class CapacityOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    stock_count: int = Field(default=5000, ge=1, le=10000)
    trading_days: int = Field(default=750, ge=1, le=780)
    batch_size: int = Field(default=10000, ge=1, le=50000)
    api_samples: int = Field(default=5, ge=1, le=100)
    timeout_seconds: float = Field(default=1800, gt=0, le=7200, allow_inf_nan=False)
    cleanup_timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    build_budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    api_budget_ms: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @classmethod
    def from_settings(cls, settings: object) -> CapacityOptions:
        return cls(**{name: getattr(settings, "capacity_" + name, default.default)
                      for name, default in cls.model_fields.items()})


def assess_budgets(build_seconds: float, api_ms: float, options: CapacityOptions) -> dict[str, object]:
    return {name: {"actual": actual, "limit": configured or recommendation,
                   "configured": configured is not None, "passed": actual <= (configured or recommendation), "unit": unit}
            for name, actual, configured, recommendation, unit in (
                ("build", build_seconds, options.build_budget_seconds, 300, "seconds"),
                ("api", api_ms, options.api_budget_ms, 500, "ms"))}


def _dates(options: CapacityOptions) -> tuple[date, ...]:
    # 合成交易日仅按工作日生成，不冒充真实交易所日历。
    current = date(2026, 8, 24)
    values = []
    while len(values) < options.trading_days:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return tuple(reversed(values))


def synthetic_price_batches(options: CapacityOptions) -> Iterator[list[dict[str, object]]]:
    dates = _dates(options)
    batch: list[dict[str, object]] = []
    for stock in range(options.stock_count):
        for day, trade_date in enumerate(dates):
            close = 10 + stock % 100 + day / 100
            batch.append({"ts_code": f"{600000 + stock:06d}.SH", "trade_date": trade_date,
                          "open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
                          "close": close, "pre_close": close - 0.1, "change": 0.1,
                          "pct_chg": 1, "vol": 100000, "amount": 100000 * close,
                          "data": {"source": "synthetic"}})
            if len(batch) == options.batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CapacityError("capacity_timeout", 3)
    return remaining


def _postgres_url(value: str | URL | None) -> URL:
    if not value:
        raise CapacityError("capacity_database_not_configured")
    try:
        url = make_url(value)
    except Exception:
        raise CapacityError("capacity_postgres_required") from None
    if url.get_backend_name() != "postgresql":
        raise CapacityError("capacity_postgres_required")
    return url.set(drivername="postgresql+psycopg")


def _engine(value: str | URL, *, readonly: bool, timeout: float) -> Engine:
    if not math.isfinite(timeout) or timeout <= 0:
        raise CapacityError("invalid_capacity_timeout")
    deadline = time.monotonic() + timeout
    url = _postgres_url(value)
    milliseconds = max(1, int(timeout * 1000))
    options = f"-c statement_timeout={milliseconds} -c lock_timeout={min(milliseconds, 5000)} -c idle_in_transaction_session_timeout={milliseconds}"
    if readonly:
        options += " -c default_transaction_read_only=on"
    engine = create_engine(url, connect_args={"connect_timeout": max(1, min(10, math.ceil(timeout))), "options": options}, pool_pre_ping=True)
    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        _remaining(deadline)
        if readonly:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    @event.listens_for(engine, "before_cursor_execute")
    def bounded_query(connection, cursor, statement, parameters, context, executemany):
        remaining_ms = str(max(1, int(_remaining(deadline) * 1000)))
        if statement.startswith("SET TRANSACTION "):
            return  # 隔离级别必须在首条查询之前设置。
        # DBAPI 直接设本事务剩余预算，避免事件递归及每条 SQL 重置总预算。
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", (remaining_ms,))
    return engine


@contextmanager
def readonly_connection(engine: Engine, *, timeout_seconds: float = 30) -> Iterator[Connection]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CapacityError("invalid_capacity_timeout")
    with engine.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        milliseconds = str(max(1, int(timeout_seconds * 1000)))
        for setting in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"):
            connection.execute(text("SELECT set_config(:name, :value, true)"), {"name": setting, "value": milliseconds})
        yield connection


def _database_report(engine: Engine, *, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    with readonly_connection(engine, timeout_seconds=timeout) as connection:
        database_bytes = connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        tables = connection.execute(text("""
            SELECT n.nspname AS schema, c.relname AS name,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   pg_relation_size(c.oid) AS heap_bytes,
                   pg_indexes_size(c.oid) AS index_bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN ('core','raw','derived','mart','ops') AND c.relkind='r'
            ORDER BY n.nspname,c.relname
        """)).mappings().all()
        indexes = connection.execute(text("""
            SELECT schemaname AS schema, relname AS table, indexrelname AS name,
                   pg_relation_size(indexrelid) AS bytes, idx_scan AS scans
            FROM pg_stat_user_indexes
            WHERE schemaname IN ('core','raw','derived','mart','ops')
            ORDER BY schemaname,relname,indexrelname
        """)).mappings().all()
        counts = {}
        quote = connection.dialect.identifier_preparer.quote
        for table in tables:
            milliseconds = str(max(1, int(_remaining(deadline) * 1000)))
            connection.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": milliseconds})
            key = f"{table['schema']}.{table['name']}"
            counts[key] = connection.exec_driver_sql(f"SELECT count(*) FROM {quote(table['schema'])}.{quote(table['name'])}").scalar_one()
        return {"databaseBytes": database_bytes, "tables": [dict(row) for row in tables],
                "indexes": [dict(row) for row in indexes], "counts": counts}


_KEY_TABLES = ("core.security", "core.daily_price", "core.daily_basic", "derived.metric_value",
               "mart.published_snapshot", "mart.stock_screener_daily")


def _key_fingerprint(engine: Engine, *, timeout: float, counts: dict[str, int] | None = None) -> dict[str, object]:
    """有限关键表行数及列元数据；不是整表内容 hash，也不声称证明所有单元格未变。"""
    with readonly_connection(engine, timeout_seconds=timeout) as connection:
        columns = [dict(row) for row in connection.execute(text("""
            SELECT n.nspname AS schema, c.relname AS table, a.attnum AS position,
                   a.attname AS column, format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid
            WHERE (n.nspname || '.' || c.relname) = ANY(:tables)
                  AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY n.nspname,c.relname,a.attnum
        """), {"tables": list(_KEY_TABLES)}).mappings()]
        existing = {f"{row['schema']}.{row['table']}" for row in columns}
        row_counts = {}
        for table in _KEY_TABLES:
            if table not in existing:
                continue
            if counts is not None and table in counts:
                row_counts[table] = counts[table]
            else:
                # 标识符完全来自本模块静态白名单。
                row_counts[table] = connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
        schema_hash = hashlib.sha256(json.dumps(columns, sort_keys=True).encode()).hexdigest()
        material = {"rowCounts": row_counts, "schemaSha256": schema_hash}
        return {**material, "fingerprintSha256": hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()}


async def _api_report(engine: Engine, *, samples: int, timeout: float) -> list[dict[str, object]]:
    import httpx
    from fastapi import FastAPI
    from app.shared.repository import SnapshotRepository
    from app.stocks.database_provider import StocksDatabaseProvider
    from app.stocks.definitions import THRESHOLD_DEFINITIONS
    from app.stocks.rea_fields import REA_BOARDS
    from app.stocks.routes import create_router

    factory = sessionmaker(engine, expire_on_commit=False)
    application = FastAPI()
    application.include_router(create_router(provider=StocksDatabaseProvider(SnapshotRepository(factory), factory)))
    base = {"filters": {"preset": None, "thresholds": {key: item.default for key, item in THRESHOLD_DEFINITIONS.items()},
                        "enabledThresholds": [], "exclusions": [], "boards": [board["id"] for board in REA_BOARDS],
                        "categories": {"majorIndustry": [], "subIndustry": [], "concept": [], "region": []}},
            "sortRules": [], "page": 1, "pageSize": 200}
    cases = [("unfiltered", base), ("board_sorted", {**base, "filters": {**base["filters"], "boards": ["sh-main"]},
                                                    "sortRules": [{"field": "pe", "direction": "desc", "enabled": True}]})]
    deadline = time.monotonic() + timeout
    reports = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://capacity.local", timeout=timeout) as client:
        for name, payload in cases:
            latencies = []
            for _ in range(samples):
                started = time.perf_counter()
                response = await asyncio.wait_for(client.post("/api/data/screener", json=payload), timeout=_remaining(deadline))
                elapsed = (time.perf_counter() - started) * 1000
                _remaining(deadline)
                if response.status_code != 200 or response.json().get("meta", {}).get("source") != "database":
                    raise CapacityError("capacity_api_failed", 3)
                body = response.json()["data"]
                latencies.append(elapsed)
            ordered = sorted(latencies)
            reports.append({"case": name, "samples": samples, "p50Ms": ordered[(samples - 1) // 2],
                            "p95Ms": ordered[math.ceil(samples * 0.95) - 1], "maxMs": max(latencies),
                            "total": body["total"], "returnedRows": len(body["items"]),
                            "transport": "in_process_asgi", "cache": "includes_first_read"})
    return reports


def capacity_status(settings: object, *, timeout_seconds: float | None = None, api_samples: int | None = None) -> dict[str, object]:
    configured = CapacityOptions.from_settings(settings)
    timeout_seconds = min(30, configured.timeout_seconds if timeout_seconds is None else timeout_seconds)
    api_samples = configured.api_samples if api_samples is None else api_samples
    if not 1 <= api_samples <= 100 or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
        raise CapacityError("invalid_capacity_configuration")
    value = getattr(settings, "database_read_url", None) or getattr(settings, "database_url", None)
    url = _postgres_url(value)
    engine = _engine(url, readonly=True, timeout=timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    try:
        database = _database_report(engine, timeout=_remaining(deadline))
        before = _key_fingerprint(engine, timeout=_remaining(deadline), counts=database["counts"])
        ready = bool(database["counts"].get("mart.stock_screener_daily", 0))
        if database["counts"].get("mart.published_snapshot", 0):
            with readonly_connection(engine, timeout_seconds=_remaining(deadline)) as connection:
                ready = ready or bool(connection.execute(text("SELECT 1 FROM mart.published_snapshot WHERE product='screener' LIMIT 1")).scalar())
        api = asyncio.run(_api_report(engine, samples=api_samples, timeout=_remaining(deadline))) if ready else []
        after = _key_fingerprint(engine, timeout=_remaining(deadline))
        return {"status": "completed", "source": "business_readonly", "database": database, "api": api,
                "apiStatus": "measured" if api else "not_published", "exitCode": 0,
                "readonlyEvidence": {"transactionReadOnly": True, "before": before, "after": after,
                                     "unchanged": before == after,
                                     "scope": "关键表行数和列元数据；复用首次业务计数，只重查关键表，不 hash 全表内容",
                                     "limitation": "不证明每个单元格未变；并发业务写入可能改变比较结果"}}
    finally:
        engine.dispose()


def _identity(url: URL) -> tuple[object, ...]:
    return url.host, url.port, url.database, url.username, url.password


@dataclass
class CapacityTarget:
    container: object = field(repr=False)
    url: URL = field(repr=False)
    owner: str = field(repr=False)
    container_id: str
    active: bool = True

    def assert_engine(self, engine: Engine) -> None:
        # 在任何连接/写入之前核验 Docker 身份、实时端口/凭据及实际 engine 目标。
        if not self.active or _identity(engine.url) != _identity(self.url) or set(engine.url.query) - {"sslmode"}:
            raise CapacityError("unsafe_capacity_target")
        wrapped = self.container.get_wrapped_container()
        wrapped.reload()
        actual = make_url(self.container.get_connection_url(driver="psycopg"))
        if (wrapped.id != self.container_id or wrapped.labels.get("danginvest.capacity.owner") != self.owner
                or wrapped.attrs["Config"]["Image"] != "postgres:17-alpine" or _identity(actual) != _identity(self.url)):
            raise CapacityError("unsafe_capacity_target")


@contextmanager
def open_capacity_target(*, deadline: float | None = None, cleanup_timeout_seconds: float = 30) -> Iterator[CapacityTarget]:
    # dev 可选依赖仅在显式 benchmark 被调用时加载，绝不自动安装。
    try:
        from testcontainers.community.postgres import PostgresContainer
        from testcontainers.core.wait_strategies import ExecWaitStrategy
        from testcontainers.core.config import testcontainers_config
        from docker.errors import NotFound
    except ImportError:
        raise CapacityError("capacity_dev_dependency_required") from None
    owner = uuid.uuid4().hex
    phase_deadline = [deadline if deadline is not None else time.monotonic() + 120]

    class BoundedPostgresContainer(PostgresContainer):
        def _connect(self):
            # pg_isready不带密码，且整个就绪等待受同一剩余预算约束。
            ExecWaitStrategy(["pg_isready", "-h", "127.0.0.1", "-U", self.username, "-d", self.dbname]).with_startup_timeout(
                _remaining(phase_deadline[0])).with_poll_interval(0.1).wait_until_ready(self)
            _remaining(phase_deadline[0])

    container = BoundedPostgresContainer("postgres:17-alpine", driver="psycopg", username="capacity_synthetic",
        password=secrets.token_urlsafe(32), dbname="capacity_synthetic",
        docker_client_kw={"timeout": min(30, _remaining(phase_deadline[0]))}).with_kwargs(labels={"danginvest.capacity.owner": owner})
    api = container.get_docker_client().client.api
    send = api.send

    def bounded_send(request, **kwargs):
        kwargs["timeout"] = _remaining(phase_deadline[0])
        return send(request, **kwargs)

    api.send = bounded_send
    target = None
    try:
        # 自有上下文已负责有限清理，不再启动预算之外的独立Ryuk辅助容器。
        # 此开关仅在本命令进程的启动调用期间暂存/恢复，不写用户环境或配置。
        original_reaper = testcontainers_config.ryuk_disabled
        try:
            testcontainers_config.ryuk_disabled = True
            container.start()
        finally:
            testcontainers_config.ryuk_disabled = original_reaper
        _remaining(phase_deadline[0])
        wrapped = container.get_wrapped_container()
        target = CapacityTarget(container, make_url(container.get_connection_url(driver="psycopg")), owner, wrapped.id)
        yield target
    finally:
        # 启动失败也尝试有限清理，但仅处理有自身owner标签的准确容器。
        phase_deadline[0] = time.monotonic() + cleanup_timeout_seconds
        if target is not None:
            target.active = False
        wrapped = container.get_wrapped_container()
        try:
            if wrapped is not None:
                wrapped.reload()
                if wrapped.labels.get("danginvest.capacity.owner") != owner or wrapped.attrs["Config"]["Image"] != "postgres:17-alpine":
                    raise CapacityError("unsafe_capacity_target")
                container.stop()
                try:
                    wrapped.reload()
                except NotFound:
                    pass
                else:
                    raise CapacityError("capacity_cleanup_failed", 3)
        finally:
            api.send = send
            container.get_docker_client().client.close()


def _migrate(engine: Engine, target: CapacityTarget) -> None:
    from alembic.config import Config
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory
    target.assert_engine(engine)
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "backend/migrations"))
    script = ScriptDirectory.from_config(config)
    # 直接绑定自有连接，绕过优先读取业务 URL 的 migrations/env.py。
    with engine.begin() as connection:
        with EnvironmentContext(config, script, fn=lambda revision, context: script._upgrade_revs("head", revision)) as environment:
            environment.configure(connection=connection, version_table_schema="public")
            environment.run_migrations()


def _seed(engine: Engine, target: CapacityTarget, options: CapacityOptions, deadline: float) -> dict[str, object]:
    started = time.perf_counter()
    as_of = _dates(options)[-1]
    batches = []
    for offset in range(0, options.stock_count, options.batch_size):
        target.assert_engine(engine)
        _remaining(deadline)
        with engine.begin() as connection:
            connection.execute(Security.__table__.insert(), [{"ts_code": f"{600000 + i:06d}.SH", "symbol": f"{600000 + i:06d}",
                "name": f"合成容量股票{i}", "exchange": "SSE", "board": "sh-main", "status": "L", "is_st": False,
                "list_date": date(2010, 1, 1), "source": "synthetic", "data": {"source": "synthetic", "industry": "合成行业", "region": "合成地区"}}
                for i in range(offset, min(options.stock_count, offset + options.batch_size))])
            connection.execute(DailyBasic.__table__.insert(), [{"ts_code": f"{600000 + i:06d}.SH", "trade_date": as_of,
                "pe": 10 + i % 20, "pb": 1 + i % 5, "total_mv": 1000000 + i, "data": {"source": "synthetic"}}
                for i in range(offset, min(options.stock_count, offset + options.batch_size))])
    for batch in synthetic_price_batches(options):
        target.assert_engine(engine)
        _remaining(deadline)
        batch_started = time.perf_counter()
        with engine.begin() as connection:
            connection.execute(DailyPrice.__table__.insert(), batch)
        batches.append({"rows": len(batch), "seconds": time.perf_counter() - batch_started})
    return {"elapsedSeconds": time.perf_counter() - started, "priceBatches": batches,
            "batchSize": options.batch_size, "method": "sqlalchemy_batched_insert"}


def capacity_benchmark(settings: object, *, options: CapacityOptions | None = None) -> dict[str, object]:
    from app.stocks.derived.inputs import CoreSnapshotRepository
    from app.marts.dto import validate_product_payload
    from app.marts.publication import SnapshotPublisher
    from app.stocks.marts.stocks import ScreenerMartBuilder

    selected = options or CapacityOptions.from_settings(settings)
    deadline = time.monotonic() + selected.timeout_seconds
    with open_capacity_target(deadline=deadline, cleanup_timeout_seconds=selected.cleanup_timeout_seconds) as target:
        engine = _engine(target.url, readonly=False, timeout=_remaining(deadline))
        reader = None
        try:
            _migrate(engine, target)
            seed = _seed(engine, target, selected, deadline)
            # 历史保留于 PG，选股读取最新报价，避免 millions ORM 实例耗尽内存。
            factory = sessionmaker(engine, expire_on_commit=False)
            started = time.perf_counter()
            snapshot = CoreSnapshotRepository(factory).load(_dates(selected)[-1], market_history=False)
            load_seconds = time.perf_counter() - started
            builder_started = time.perf_counter()
            candidate = ScreenerMartBuilder(factory).build(snapshot, "capacity-synthetic-v1")
            builder_seconds = time.perf_counter() - builder_started
            build_seconds = time.perf_counter() - started
            _remaining(deadline)
            if validate_product_payload("screener", candidate.payload):
                raise CapacityError("capacity_invalid_screener_payload", 3)
            target.assert_engine(engine)
            published_started = time.perf_counter()
            SnapshotPublisher(factory).publish_many((candidate,), "capacity-synthetic-v1", ())
            publish_seconds = time.perf_counter() - published_started
            reader = _engine(target.url, readonly=True, timeout=min(120, _remaining(deadline)))
            api = asyncio.run(_api_report(reader, samples=selected.api_samples, timeout=min(120, _remaining(deadline))))
            database = _database_report(reader, timeout=min(120, _remaining(deadline)))
            counts = database["counts"]
            actual = counts["core.daily_price"]
            if actual != selected.stock_count * selected.trading_days or len(candidate.rows.screener) != selected.stock_count:
                raise CapacityError("capacity_scale_mismatch", 3)
            budgets = assess_budgets(build_seconds, max(item["p95Ms"] for item in api), selected)
            passed = all(item["passed"] for item in budgets.values())
            report = {"status": "completed" if passed else "budget_exceeded", "source": "synthetic", "exitCode": 0 if passed else 4,
                      "scale": {"securityRows": counts["core.security"], "dailyPriceRows": actual, "tradingDays": selected.trading_days,
                                "dailyBasicRows": counts["core.daily_basic"]}, "insert": seed,
                      "build": {"mode": "latest_quote_screener", "marketHistory": False,
                                "elapsedSeconds": build_seconds, "loadSeconds": load_seconds, "builderSeconds": builder_seconds,
                                "publishSeconds": publish_seconds, "screenerRows": len(candidate.rows.screener), "loadedPriceRows": len(snapshot.daily_prices)},
                      "database": database, "api": api, "budgets": budgets,
                      "limitations": ["合成工作日日历及价格；非供应商历史", "仅 latest daily_basic；未生成完整财报/派生指标历史",
                                       "latest_quote_screener 仅载入每股最新报价；不能视为 full history、PEpercentile、完整 BuildAndPublish 或工作台全部蜡烛基准",
                                       "预算构建包含最新报价 Repository + 真实 ScreenerMartBuilder；发布另计",
                                       "API 为进程内真实路由/DB/序列化，不含网络及并发负载"]}
        finally:
            if reader is not None:
                reader.dispose()
            engine.dispose()
    report["cleanupVerified"] = True
    return report
