"""项目拥有的测试进程入口：禁用用户配置、真实HTTP和未登记数据库。"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
import tempfile
import subprocess
import json
from contextvars import ContextVar
from functools import wraps
from weakref import WeakSet


def isolate_extension_config(directory: Path) -> None:
    """用户启用采集不改变旧测试选组；显式传文件的用例仍可测试启用状态。"""
    from app.macro.extensions import config
    value = config.load_extension_config().model_dump()
    for group in value['groups'].values():
        group['collectionEnabled'] = False
    for sample in value.get('contractSamples', {}).values():
        sample['enabled'] = False
    path = directory / 'macro-extensions.json'
    path.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False), encoding='utf-8')
    config.DEFAULT_EXTENSION_CONFIG_FILE = path
    # 用户新增组合采集组不改变既有测试选组；专项用例显式启用。
    from app.portfolio import routine
    portfolio = json.loads(routine.CONFIG_FILE.read_text(encoding='utf-8'))
    portfolio.setdefault('routine', {})['enabled'] = False
    portfolio_path = directory / 'portfolio.json'
    portfolio_path.write_text(json.dumps(portfolio,ensure_ascii=True,allow_nan=False),encoding='utf-8')
    routine.CONFIG_FILE = portfolio_path
    from app.stocks import financial_supply
    financial=json.loads(financial_supply.CONFIG_FILE.read_text(encoding='utf-8'))
    financial['dataSupply']['enabled']=False
    financial['announcementIndex']['enabled']=False
    financial.setdefault('valuationSupply',{})['enabled']=False
    financial.setdefault('businessSupply',{})['enabled']=False
    financial.setdefault('macroHistoryBridge',{})['enabled']=False
    financial_path=directory/'financial-indicators.json'
    financial_path.write_text(json.dumps(financial,ensure_ascii=True,allow_nan=False),encoding='utf-8')
    financial_supply.CONFIG_FILE=financial_path
    from app.macro import stock_history_policy
    stock_history_policy.CONFIG_FILE=financial_path
    from app.stocks import valuation_supply
    valuation_supply.CONFIG_FILE=financial_path
    from app.stocks import business_supply
    business_supply.CONFIG_FILE=financial_path


def isolate_official_member_config(directory: Path) -> None:
    """官方策略单独关闭；保留旧证据策略和其冻结指纹，不改用户文件。"""
    from app.macro import enterprise_target_catalog as catalog
    value = json.loads(catalog.DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE.read_text(encoding='utf-8'))
    if 'officialEvidence' not in value:
        return
    value['officialEvidence']['collectionEnabled'] = False
    path = directory / 'enterprise-targets.json'
    path.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False), encoding='utf-8')
    catalog.DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE = path


def main() -> None:
    args = sys.argv[1:]
    child_url = None
    if args and args[0] == "--registered-child":
        args.pop(0)
        child_url = json.loads(sys.stdin.readline())["url"]
    postgres = bool(args and args[0] == "--postgres")
    if postgres:
        args.pop(0)
    if not args or (args[0] != "pytest" and not args[0].startswith("backend.tests.")):
        raise SystemExit("Only pytest or backend.tests modules are allowed")
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "backend"), str(root / "packages/news-collector/src")]
    for name in tuple(os.environ):
        if name.upper().startswith(("DANG_", "COMPOSE_")):
            del os.environ[name]
    os.environ["DANG_RUN_POSTGRES_TESTS"] = "1" if postgres else "0"
    # 测试应用只允许内存SQLite；绝不继承业务数据库连接或演示运行模式。
    os.environ["DANG_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

    import app.config as config
    import httpx
    import requests
    from sqlalchemy import event
    from sqlalchemy.engine import Connection, Dialect, Engine, make_url
    from testcontainers.community.postgres import PostgresContainer

    from backend.tests.support import postgres as pg_support

    violations: list[str] = []
    allowed_databases: set[tuple[object, ...]] = set()
    pg_support.allowed_databases = allowed_databases
    pg_support.enabled = postgres
    if child_url:
        child = make_url(child_url)
        allowed_databases.add((child.host, child.port, child.database, child.username, child.password))
        pg_support.child_url = child_url
    sqlite_engines: WeakSet[Engine] = WeakSet()
    sqlite_connections: WeakSet[Connection] = WeakSet()

    def remember_sqlite_connection(connection: Connection) -> None:
        if connection.dialect.name == "sqlite":
            sqlite_connections.add(connection)
            sqlite_engines.add(connection.engine)

    def deny_http(*args: object, **kwargs: object) -> object:
        violations.append("real HTTP")
        raise AssertionError("Offline tests forbid real HTTP")

    async def deny_async_http(*args: object, **kwargs: object) -> object:
        return deny_http()

    original_request = requests.Session.request
    original_send = requests.Session.send

    def controlled_request(session: requests.Session, *args: object, **kwargs: object) -> object:
        if postgres and type(session).__module__.startswith("docker."):
            return original_request(session, *args, **kwargs)
        return deny_http()

    def controlled_send(session: requests.Session, *args: object, **kwargs: object) -> object:
        if postgres and type(session).__module__.startswith("docker."):
            return original_send(session, *args, **kwargs)
        return deny_http()

    original_url = PostgresContainer.get_connection_url

    def disposable_url(container: PostgresContainer, *args: object, **kwargs: object) -> str:
        if not postgres or container.image != "postgres:17-alpine":
            violations.append("unregistered container")
            raise AssertionError("Only explicitly enabled disposable PostgreSQL 17 is allowed")
        value = original_url(container, *args, **kwargs)
        url = make_url(value)
        allowed_databases.add((url.host, url.port, url.database, url.username, url.password))
        return value

    def controlled_database(dialect: Dialect, record: object, args: object, params: dict[str, object]) -> None:
        if dialect.name == "sqlite":
            return
        identity = (params.get("host"), params.get("port"), params.get("dbname"), params.get("user"), params.get("password"))
        if (postgres or child_url) and dialect.name == "postgresql" and identity in allowed_databases:
            return
        violations.append("unregistered database")
        raise AssertionError("Offline tests forbid unregistered databases")

    httpx.HTTPTransport.handle_request = deny_http
    httpx.AsyncHTTPTransport.handle_async_request = deny_async_http
    requests.Session.request = controlled_request
    requests.Session.send = controlled_send
    PostgresContainer.get_connection_url = disposable_url
    event.listen(Dialect, "do_connect", controlled_database)
    event.listen(Engine, "engine_connect", remember_sqlite_connection)

    # 备份多了一条不经过 SQLAlchemy 的 libpq 子进程通路，必须在执行前审计。
    import app.ops.backup.process as backup_process
    import app.ops.backup.tools as backup_tools
    import app.ops.backup.targets as backup_targets
    import app.ops.backup.api_probe as backup_api
    import app.ops.backup.service as backup_service
    authorized_tool = ContextVar("authorized_backup_tool", default=False)
    original_process = backup_process.run_process
    original_popen = subprocess.Popen

    def denied_tool() -> None:
        violations.append("unregistered backup tool")
        raise AssertionError("Offline tests forbid unregistered backup tools/targets")

    def is_database_tool(argv: object) -> bool:
        if not isinstance(argv, (list, tuple)) or not argv:
            return False
        name = Path(str(argv[0])).name.lower().removesuffix(".exe")
        if name == "docker" and len(argv) > 1 and argv[1] == "compose" and "config" in argv[2:]:
            return False  # 既有纯配置渲染，不启动 daemon 资源或连接数据库。
        return name in {"docker", "pg_dump", "pg_restore", "psql", "pg_isready", "postgres"}

    def controlled_popen(argv: object, *args: object, **kwargs: object):
        if is_database_tool(argv) and not authorized_tool.get():
            denied_tool()
        return original_popen(argv, *args, **kwargs)

    def controlled_process(argv: object, **kwargs: object):
        if is_database_tool(argv) and not authorized_tool.get():
            denied_tool()
        return original_process(argv, **kwargs)

    def registered_target(target: object) -> None:
        identity = (target.host, target.port, target.database, target.username, target.password.get_secret_value())
        if not postgres or identity not in allowed_databases:
            denied_tool()

    def protect_method(cls: type, name: str) -> None:
        original = getattr(cls, name)
        @wraps(original)
        def guarded(instance: object, *args: object, **kwargs: object):
            # 单元测试可替换终端依赖；这不会授权任何真实 Popen。
            if backup_tools.run_process is not controlled_process:
                return original(instance, *args, **kwargs)
            if name in {"dump", "restore"}:
                registered_target(args[0] if args else kwargs["target"])
            elif not postgres:
                denied_tool()
            if isinstance(instance, backup_tools.DockerTools) and instance.options.docker_image != "postgres:17-alpine":
                denied_tool()
            token = authorized_tool.set(True)
            try:
                return original(instance, *args, **kwargs)
            finally:
                authorized_tool.reset(token)
        setattr(cls, name, guarded)

    original_probe = backup_api.probe_restored_api
    @wraps(original_probe)
    def controlled_probe(target: object, *args: object, **kwargs: object):
        registered_target(target)
        token = authorized_tool.set(True)
        try:
            return original_probe(target, *args, **kwargs)
        finally:
            authorized_tool.reset(token)

    backup_process.run_process = controlled_process
    backup_tools.run_process = controlled_process
    backup_targets.run_process = controlled_process
    backup_api.run_process = controlled_process
    backup_service.run_process = controlled_process
    backup_api.probe_restored_api = controlled_probe
    backup_service.probe_restored_api = controlled_probe
    subprocess.Popen = controlled_popen
    for cls in (backup_tools.NativeTools, backup_tools.DockerTools):
        for name in ("versions", "list_archive", "dump", "restore"):
            protect_method(cls, name)
    with tempfile.TemporaryDirectory(prefix="backend-tests-") as directory:
        config.PROJECT_ENV_FILE = Path(directory) / "missing.env"
        news_config = Path(directory) / 'news-disabled.json'
        news_config.write_text('{"enabled": false}', encoding='utf-8')
        os.environ['DANG_NEWS_CONFIG_PATH'] = str(news_config)
        os.environ['DANG_NEWS_STATE_PATH'] = str(Path(directory) / 'absent-legacy.sqlite')
        isolate_extension_config(Path(directory))
        isolate_official_member_config(Path(directory))
        if args[0] == "pytest":
            # 临时目录/缓存属于本次进程，不访问旧用户pytest目录；后置CLI选项仍可显式覆盖。
            args = [args[0], "--basetemp", str(Path(directory) / "pytest"),
                "-o", f"cache_dir={Path(directory) / 'pytest-cache'}", *args[1:]]
        sys.argv = args
        try:
            runpy.run_module(args[0], run_name="__main__", alter_sys=True)
        finally:
            try:
                # Windows不能删除仍被测试池持有的SQLite文件；只释放本进程已跟踪的测试资源。
                try:
                    for connection in tuple(sqlite_connections):
                        if not connection.closed:
                            connection.close()
                finally:
                    for engine in tuple(sqlite_engines):
                        engine.dispose()
            finally:
                print(f"Offline test isolation: user dotenv disabled; real HTTP/business DB violations={len(violations)}; disposable PG identities={len(allowed_databases)}", file=sys.stderr, flush=True)
                if violations:
                    raise SystemExit(97)


if __name__ == "__main__":
    main()
