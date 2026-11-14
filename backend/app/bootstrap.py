from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.shared.cli import SafeArgumentParser
from app.db.models import PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot
from app.db.session import create_engine_and_session
from app.marts.dto import validate_product_payload
from app.shared.errors import DataServiceError
from app.shared.validation import normalize_stock_symbol


CORE_DATASETS = (
    "security", "daily", "daily_basic", "adj_factor",
    "income", "balance_sheet", "cashflow", "financial_indicator",
)
GLOBAL_PRODUCTS = (
    "market-overview", "market-explorer", "macro-transmission",
    "macro-insights", "screener", "portfolio",
)
MACRO_PRODUCTS = frozenset({"macro-transmission", "macro-insights"})


class BootstrapConfigurationError(ValueError):
    pass


class BootstrapCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    version: str | None
    problems: tuple[str, ...]


class SnapshotReadinessChecker:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def check(self, symbols: tuple[str, ...]) -> ReadinessResult:
        products = expected_products(symbols)
        problems: list[str] = []
        with self._factory() as session:
            pointers = {
                row.product: row
                for row in session.scalars(
                    select(PublishedSnapshot).where(
                        PublishedSnapshot.product.in_(products)
                    )
                ).all()
            }
            for product in products:
                pointer = pointers.get(product)
                if pointer is None:
                    if product in MACRO_PRODUCTS and not (MACRO_PRODUCTS & pointers.keys()):
                        continue
                    problems.append(f"missing published product: {product}")
                    continue
                payload_errors = validate_product_payload(product, pointer.payload)
                if not pointer.version.strip():
                    problems.append(f"empty published version: {product}")
                if not isinstance(pointer.payload, dict) or not pointer.payload:
                    problems.append(f"empty published payload: {product}")
                elif payload_errors:
                    problems.append(
                        f"invalid published payload: {product} "
                        f"({', '.join(payload_errors)})"
                    )
            versions = {row.version for product, row in pointers.items() if product not in MACRO_PRODUCTS and row.version.strip()}
            if len(versions) > 1:
                problems.append("published products use multiple versions")
            version = next(iter(versions)) if len(versions) == 1 else None
            macro_versions = {row.version for product, row in pointers.items() if product in MACRO_PRODUCTS}
            if len(macro_versions) > 1:
                problems.append("macro products use multiple versions")
            page_pairs = set(session.execute(
                select(PageSnapshot.product, PageSnapshot.snapshot_version).where(
                    PageSnapshot.product.in_(products)
                )
            ).all())
            for product, pointer in pointers.items():
                if (product, pointer.version) not in page_pairs:
                    problems.append(f"missing page snapshot: {product}@{pointer.version}")
            if version is not None:
                workbench_pairs = set(session.execute(
                    select(
                        StockWorkbenchSnapshot.ts_code,
                        StockWorkbenchSnapshot.snapshot_version,
                    ).where(StockWorkbenchSnapshot.ts_code.in_(symbols))
                ).all())
                for symbol in symbols:
                    if (symbol, version) not in workbench_pairs:
                        problems.append(f"missing workbench snapshot: {symbol}@{version}")
        return ReadinessResult(
            ready=not problems,
            version=version if not problems else None,
            problems=tuple(problems),
        )


class CliCommandRunner(Protocol):
    def run(self, args: tuple[str, ...]) -> int:
        pass


class ReadinessChecker(Protocol):
    def check(self, symbols: tuple[str, ...]) -> ReadinessResult:
        pass


class SubprocessCliRunner:
    def run(self, args: tuple[str, ...]) -> int:
        result = subprocess.run(
            (sys.executable, "-m", "app.cli.main", *args),
            check=False,
        )
        return result.returncode


@dataclass(frozen=True)
class BootstrapOptions:
    symbols: tuple[str, ...]
    as_of: date
    force: bool
    mode: str

    @classmethod
    def from_settings(
        cls, settings: Settings, *, now: datetime | None = None,
    ) -> "BootstrapOptions":
        raw_symbols = settings.bootstrap_symbols.split(",")
        if not raw_symbols or any(not value.strip() for value in raw_symbols):
            raise BootstrapConfigurationError("DANG_BOOTSTRAP_SYMBOLS 不能为空")
        try:
            symbols = tuple(
                dict.fromkeys(normalize_stock_symbol(value) for value in raw_symbols)
            )
        except DataServiceError as exc:
            raise BootstrapConfigurationError(str(exc)) from exc
        try:
            timezone = ZoneInfo(settings.sync_timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise BootstrapConfigurationError(
                "DANG_SYNC_TIMEZONE 不合法"
            ) from exc
        current = now.astimezone(timezone) if now is not None else datetime.now(timezone)
        return cls(
            symbols=symbols,
            as_of=settings.bootstrap_as_of or current.date(),
            force=settings.bootstrap_force,
            mode=settings.tushare_mode,
        )


def expected_products(symbols: tuple[str, ...]) -> tuple[str, ...]:
    return GLOBAL_PRODUCTS + tuple(f"stock-workbench:{symbol}" for symbol in symbols)


def build_backfill_args(options: BootstrapOptions) -> tuple[str, ...]:
    args = ["backfill", "--mode", options.mode, "--as-of", options.as_of.isoformat()]
    for dataset in CORE_DATASETS:
        args.extend(("--dataset", dataset))
    for symbol in options.symbols:
        args.extend(("--symbol", symbol))
    return tuple(args)


def build_publish_args(options: BootstrapOptions) -> tuple[str, ...]:
    args = ["build-and-publish", "--as-of", options.as_of.isoformat()]
    for symbol in options.symbols:
        args.extend(("--symbol", symbol))
    return tuple(args)


@dataclass(frozen=True)
class BootstrapReport:
    status: Literal["completed", "skipped"]
    version: str
    symbols: tuple[str, ...]
    as_of: date


class BootstrapRunner:
    def __init__(
        self,
        readiness: ReadinessChecker,
        commands: CliCommandRunner,
    ) -> None:
        self._readiness = readiness
        self._commands = commands

    def run(self, options: BootstrapOptions) -> BootstrapReport:
        initial = self._readiness.check(options.symbols)
        if initial.ready and not options.force:
            if initial.version is None:
                raise BootstrapCommandError("ready result has no version")
            return BootstrapReport(
                "skipped", initial.version, options.symbols, options.as_of,
            )
        backfill_exit = self._commands.run(build_backfill_args(options))
        if backfill_exit != 0:
            raise BootstrapCommandError(
                f"backfill failed with exit code {backfill_exit}"
            )
        publish_exit = self._commands.run(build_publish_args(options))
        if publish_exit != 0:
            raise BootstrapCommandError(
                f"build-and-publish failed with exit code {publish_exit}"
            )
        final = self._readiness.check(options.symbols)
        if not final.ready or final.version is None:
            details = "; ".join(final.problems) or "unknown readiness failure"
            raise BootstrapCommandError(f"bootstrap final check failed: {details}")
        return BootstrapReport(
            "completed", final.version, options.symbols, options.as_of,
        )


def _redact(message: str, settings: Settings | None) -> str:
    secrets: list[str] = []
    if settings is not None:
        if settings.tushare_token is not None:
            secrets.append(settings.tushare_token.get_secret_value())
        for database_url in (
            settings.database_url,
            settings.database_read_url,
        ):
            if not database_url:
                continue
            try:
                password = make_url(database_url).password
            except Exception:
                password = None
            if password:
                secrets.append(password)
            secrets.append(database_url)
    sanitized = message
    for secret in sorted(
        (value for value in secrets if value), key=len, reverse=True,
    ):
        sanitized = sanitized.replace(secret, "***")
    return sanitized


def run_from_settings(settings: Settings, *, force: bool = False) -> BootstrapReport:
    if settings.bootstrap_force and not force:
        raise BootstrapConfigurationError("DANG_BOOTSTRAP_FORCE requires explicit --force; configuration alone does not authorize rebuild")
    if settings.data_backend != "database":
        raise BootstrapConfigurationError(
            "bootstrap requires DANG_DATA_BACKEND=database"
        )
    if not settings.database_url:
        raise BootstrapConfigurationError("bootstrap requires DANG_DATABASE_URL")
    if (
        settings.tushare_token is None
        or not settings.tushare_token.get_secret_value().strip()
    ):
        raise BootstrapConfigurationError("bootstrap requires DANG_TUSHARE_TOKEN")
    options = BootstrapOptions.from_settings(settings)
    if force:
        options = replace(options, force=True)
    engine, factory = create_engine_and_session(settings.database_url)
    try:
        return BootstrapRunner(
            SnapshotReadinessChecker(factory), SubprocessCliRunner(),
        ).run(options)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(prog="app.bootstrap", description="按配置初始化后端；--force才明确授权重新采集及发布。")
    parser.add_argument("--force", action="store_true", help="本次明确授权强制重建")
    arguments = parser.parse_args([] if argv is None else argv)
    try:
        settings = Settings.from_env()
    except Exception as exc:
        payload = {
            "status": "error",
            "errorType": type(exc).__name__,
            "message": "bootstrap configuration is invalid",
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        report = run_from_settings(settings, force=arguments.force)
        print(json.dumps(asdict(report), ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        payload = {
            "status": "error",
            "errorType": type(exc).__name__,
            "message": _redact(str(exc), settings),
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
