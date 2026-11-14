from __future__ import annotations

from app.macro.freshness import observation_expired

import json
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import typer
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import Settings, configured_tushare_token
from app.shared.cli import SafeTyperGroup
from app.ops.observability import emit_report, install_cli_observability
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import TushareError
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.connectors.tushare.strategies import (
    BACKFILL_DATASETS,
    VIP_API_NAMES,
    Dataset,
    SyncScope,
    TushareMode,
    build_request_plans,
)
from app.connectors.external_files.reader import ExternalFileReader, ImportValidationFailed
from app.db.session import create_engine_and_session
from app.ingestion.external_import import ExternalImporter
from app.ingestion.pagination import PaginatedPlanRunner, PaginatedResult
from app.ingestion.pipeline import BatchResult, IngestionPipeline
from app.stocks.jobs.backfill import BackfillJob, BackfillOptions
from app.stocks.jobs.backfill_history import SqlBackfillHistory
from app.jobs.build_and_publish import (
    BuildAndPublishJob,
    BuildAndPublishOptions,
    DefaultMartBuilders,
)
from app.jobs.build_history import SqlBuildHistory
from app.jobs.cursors import SqlJobCursorRepository
from app.stocks.jobs.daily_sync import DailySyncOptions
from app.stocks.jobs.daily_sync_repository import DailySyncRepository
from app.stocks.jobs.daily_sync_runtime import (
    DailySyncRuntimeError,
    open_daily_sync_runtime,
    redact_sensitive_values,
)
from app.connectors.macro.contracts import MacroFetchWindow, ProviderAuthError, ProviderPermissionError
from app.connectors.macro.fred import CalendarFredMacroProvider
from app.connectors.macro.transport import MacroHttpTransport
from app.connectors.macro.tushare import TushareMacroProvider
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository, MacroSeriesStateView
from app.macro.jobs.macro_sync import MacroPublishReport, MacroSeriesOutcome, MacroSeriesStatus, MacroSyncReport
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntimeError, open_macro_sync_runtime
from app.macro.jobs.macro_catchup_plan import read_macro_catchup_plan
from app.macro.jobs.macro_catchup import run_macro_catchup
from app.content.marts.content_runtime import rebuild_content_marts
from app.macro.jobs.us_rates_sync_runtime import SERIES_IDS as US_RATE_SERIES_IDS, open_us_rates_sync_runtime
from app.macro.jobs.nbs_sync_runtime import SERIES_IDS as NBS_SERIES_IDS, open_nbs_sync_runtime
from app.macro.jobs.tushare_macro_sync_runtime import SERIES_IDS as TUSHARE_MACRO_SERIES_IDS, open_tushare_macro_sync_runtime
from app.macro.jobs.fred_macro_sync_runtime import SERIES_IDS as FRED_MACRO_SERIES_IDS, open_fred_macro_sync_runtime
from app.macro.jobs.macro_history_runtime import open_macro_history_runtime, read_macro_history_status
from app.macro.jobs.govbond_history_runtime import open_govbond_history_runtime, read_govbond_history_status
from app.macro.jobs.dr007_history_runtime import open_dr007_history_runtime, read_dr007_history_status
from app.macro.jobs.usdcny_history_runtime import open_usdcny_history_runtime, read_usdcny_history_status
from app.macro.jobs.govbond_sync_runtime import open_govbond_sync_runtime
from app.macro.jobs.dr007_sync_runtime import open_dr007_sync_runtime
from app.macro.jobs.macro_history_reconcile import reconcile_macro_history
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.db.models import MacroObservation
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder, MacroRepository, select_visible_observations
from app.jobs.freshness import FreshnessService
from app.jobs.failure_retries import (
    FailureQuery,
    RetryFailuresJob,
    RetryOutcome,
    SqlFailureRepository,
)
from app.stocks.jobs.scheduler_planning import CatchupPlanner, ExpectedBusinessDateResolver
from app.jobs.scheduler_state import SchedulerStateRepository
from app.jobs.sync_status import MacroStatusReader, SyncStatusReport, SyncStatusService
from app.stocks.jobs.universe_backfill_runtime import (
    UniverseBackfillRuntimeError,
    open_universe_backfill_runtime,
)
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.stocks.derived.pipeline import DerivedPipeline
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.shared.validation import normalize_stock_symbol
from app.stocks.universes import UniverseConfigError, UniverseResolver
from app.stocks.universes.cli_selection import resolve_cli_selection


app = typer.Typer(cls=SafeTyperGroup, add_completion=False, no_args_is_help=True)


@app.command("rebuild-workbench-risks")
def rebuild_workbench_risks_command(execute: bool = typer.Option(False, "--execute")):
    """预演或执行工作台十二张风险卡的发布与历史清理。"""
    from app.db.session import create_engine_and_session
    from app.stocks.marts.risk_rebuild import rebuild_workbench_risks
    engine, factory = create_engine_and_session(Settings.from_env().database_url)
    try:
        report = rebuild_workbench_risks(factory, execute=execute)
        _echo(json.dumps(report, ensure_ascii=False))
        if report["status"] == "locked":
            raise typer.Exit(1)
    finally:
        engine.dispose()


def _echo(value: object, *args, **kwargs) -> None:
    """统一既有JSON报告接线；stderr文本和普通帮助输出不改变。"""
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and not args and set(kwargs) <= {"err"}:
            emit_report(payload, err=bool(kwargs.get("err", False)))
            return
    typer.echo(value, *args, **kwargs)


def _batch_payload(result: BatchResult) -> dict[str, object]:
    return {
        "batchId": result.batch_id,
        "dataset": result.dataset.value,
        "receivedCount": result.received_count,
        "rowCount": result.row_count,
        "rejectedCount": result.rejected_count,
    }


def _plan_payload(result: PaginatedResult) -> dict[str, object]:
    return {
        "dataset": result.dataset.value,
        "requestKey": result.request_key,
        "pages": result.pages,
        "requestCount": result.request_count,
        "resumedFromOffset": result.resumed_from_offset,
        "rowCount": result.row_count,
        "rejectedCount": result.rejected_count,
        "batchIds": [batch.batch_id for batch in result.batches],
        "completed": result.completed,
    }


def _batch_results(results: tuple[object, ...]) -> list[BatchResult]:
    batches: list[BatchResult] = []
    for result in results:
        if isinstance(result, PaginatedResult):
            batches.extend(result.batches)
        elif isinstance(result, BatchResult):
            batches.append(result)
    return batches


def _redact(message: str, token: str) -> str:
    return _redact_sensitive(message, token)


def _redact_sensitive(message: str, *secrets: str) -> str:
    return redact_sensitive_values(message, *secrets)


def _retry_outcome_payload(outcome: RetryOutcome, token: str) -> dict[str, object]:
    return {
        "failureId": outcome.failure_id,
        "jobRunId": outcome.job_run_id,
        "dataset": outcome.dataset.value,
        "requestKey": outcome.request_key,
        "status": outcome.status,
        "attempts": outcome.attempts,
        "errorCode": outcome.error_code,
        "message": _redact(outcome.message or "", token) if outcome.message is not None else None,
        "retryable": outcome.retryable,
    }


@contextmanager
def build_sync_status_service(settings: Settings) -> Iterator[SyncStatusService]:
    if not settings.database_url:
        raise RuntimeError("sync-status requires DANG_DATABASE_URL")
    database_url = settings.database_read_url or settings.database_url
    engine: Engine | None = None
    try:
        engine, factory = create_engine_and_session(database_url)
        daily_repository = DailySyncRepository(factory)
        universe = UniverseResolver(
            active_symbols=daily_repository.active_symbols,
            security_states=daily_repository.security_states,
        ).resolve(
            settings.sync_universe,
            universe_file=settings.sync_universe_file,
        )
        state = SchedulerStateRepository(factory)
        yield SyncStatusService(
            state=state,
            resolver=ExpectedBusinessDateResolver(
                state,
                settings.sync_timezone,
                settings.sync_hour,
                settings.sync_minute,
            ),
            planner=CatchupPlanner(
                state,
                universe,
                max_days=settings.sync_catchup_max_days,
            ),
            freshness=FreshnessService(factory, timezone=settings.sync_timezone),
            universe=universe,
            timezone=settings.sync_timezone,
            heartbeat_timeout=timedelta(
                seconds=settings.scheduler_heartbeat_timeout_seconds
            ),
            catchup_max_days=settings.sync_catchup_max_days,
            macro_status=MacroStatusReader(factory, settings),
        )
    finally:
        if engine is not None:
            engine.dispose()


def _sync_status_unavailable_payload(
    checked_at: datetime,
    timezone: str,
) -> dict[str, object]:
    return {
        "status": "unavailable",
        "checkedAt": checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "timezone": timezone,
        "error": {
            "code": "SYNC_STATUS_UNAVAILABLE",
            "message": "sync status unavailable",
        },
    }


def _sync_status_exit_code(status: str) -> int:
    try:
        return {"ok": 0, "degraded": 1, "unavailable": 3}[status]
    except KeyError:
        raise RuntimeError(f"unsupported sync status: {status}") from None


@app.command("sync-status")
def sync_status() -> None:
    checked_at = datetime.now(UTC)
    timezone = "Asia/Shanghai"
    database_url = ""
    database_read_url = ""
    token = ""
    try:
        try:
            settings = Settings.from_env()
        except Exception as exc:
            _echo(
                json.dumps(
                    _sync_status_unavailable_payload(checked_at, timezone),
                    ensure_ascii=False,
                )
            )
            _echo(
                f"{type(exc).__name__}: sync-status settings could not be loaded",
                err=True,
            )
            raise typer.Exit(code=3) from exc
        timezone = settings.sync_timezone
        database_url = settings.database_url or ""
        database_read_url = settings.database_read_url or ""
        if settings.tushare_token is not None:
            token = settings.tushare_token.get_secret_value()
        if not database_url:
            raise RuntimeError("sync-status requires DANG_DATABASE_URL")
        with build_sync_status_service(settings) as service:
            report: SyncStatusReport = service.check(checked_at)
        exit_code = _sync_status_exit_code(report.status)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False))
        if exit_code:
            raise typer.Exit(code=exit_code)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(
            json.dumps(
                _sync_status_unavailable_payload(checked_at, timezone),
                ensure_ascii=False,
            )
        )
        _echo(
            _redact_sensitive(
                f"{type(exc).__name__}: {exc}",
                token,
                database_url,
                database_read_url,
            ),
            err=True,
        )
        raise typer.Exit(code=3) from exc


@app.command("backfill-universe")
def backfill_universe(
    confirm_full_market: bool = typer.Option(False, "--confirm-full-market"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    database_url = ""
    database_password = ""
    token = ""
    settings_error_message: str | None = None
    settings_exit: typer.Exit | None = None
    try:
        settings = Settings.from_env()
    except Exception:
        settings_error_message = (
            "backfill-universe settings could not be loaded"
        )
        settings_exit = typer.Exit(code=3)
    if settings_exit is not None:
        _echo(settings_error_message, err=True)
        raise settings_exit from None

    safe_error_message: str | None = None
    safe_exit: typer.Exit | None = None
    try:
        database_url = settings.database_url or ""
        if database_url:
            try:
                database_password = make_url(database_url).password or ""
            except Exception:
                pass
        if settings.tushare_token is not None:
            token = settings.tushare_token.get_secret_value()

        if settings.sync_universe == "all" and not confirm_full_market:
            _echo(
                "all universe requires --confirm-full-market",
                err=True,
            )
            raise typer.Exit(code=2)
        if settings.sync_universe != "all" and confirm_full_market:
            _echo(
                "--confirm-full-market is only valid for all universe",
                err=True,
            )
            raise typer.Exit(code=2)
        if settings.backfill_as_of is None:
            _echo(
                "backfill-universe requires DANG_BACKFILL_AS_OF",
                err=True,
            )
            raise typer.Exit(code=2)

        with open_universe_backfill_runtime(settings) as runtime:
            options = replace(runtime.options, force=force)
            plan = runtime.job.plan(options)
            _echo(
                json.dumps(plan.preflight_payload(), ensure_ascii=False),
                err=True,
            )
            report = runtime.job.run(options)

        _echo(json.dumps(report.to_payload(), ensure_ascii=False))
        exit_code = {
            "completed": 0,
            "skipped": 0,
            "partial": 4,
            "failed": 4,
            "locked": 5,
        }[report.status]
        if exit_code:
            raise typer.Exit(code=exit_code)
    except typer.Exit as error:
        safe_exit = error
    except (UniverseConfigError, UniverseBackfillRuntimeError) as error:
        safe_error_message = redact_sensitive_values(
            str(error),
            token,
            database_url,
            database_password,
        )
        safe_exit = typer.Exit(code=3)
    except Exception:
        safe_error_message = "backfill-universe failed"
        safe_exit = typer.Exit(code=4)

    if safe_error_message is not None:
        _echo(safe_error_message, err=True)
    if safe_exit is not None:
        raise safe_exit from None


@app.command()
def backfill(
    mode: str = typer.Option(..., help="Tushare 权限模式：standard 或 vip"),
    as_of: str = typer.Option(..., help="YYYY-MM-DD"),
    financial_start: str | None = typer.Option(None, help="财务和公司行为回填起始日（YYYY-MM-DD）"),
    publish: bool = typer.Option(False, "--publish", help="成功回填后，只重建受影响的已发布工作台指标卡"),
    dataset: list[str] | None = typer.Option(None),
    symbol: list[str] | None = typer.Option(None),
) -> None:
    if mode not in {"standard", "vip"}:
        _echo("--mode 必须是 standard 或 vip", err=True)
        raise typer.Exit(code=2)
    try:
        parsed_as_of = date.fromisoformat(as_of)
    except ValueError:
        _echo("--as-of 必须是 YYYY-MM-DD", err=True)
        raise typer.Exit(code=2) from None
    try:
        parsed_financial_start = date.fromisoformat(financial_start) if financial_start else None
    except ValueError:
        _echo("--financial-start 必须是 YYYY-MM-DD", err=True)
        raise typer.Exit(code=2) from None
    if parsed_financial_start and parsed_financial_start > parsed_as_of:
        _echo("--financial-start 不能晚于 --as-of", err=True)
        raise typer.Exit(code=2)

    if not dataset:
        _echo("backfill requires at least one --dataset", err=True)
        raise typer.Exit(code=2)
    try:
        datasets = tuple(Dataset(value) for value in dataset)
    except ValueError as exc:
        _echo(f"unsupported dataset: {exc.args[0]}", err=True)
        raise typer.Exit(code=2) from None
    unsupported_dataset = next(
        (value for value in datasets if value not in BACKFILL_DATASETS),
        None,
    )
    if unsupported_dataset is not None:
        _echo(f"unsupported dataset: {unsupported_dataset.value}", err=True)
        raise typer.Exit(code=2)

    mode_value = TushareMode(mode)
    symbols = tuple(symbol or ())
    global_datasets = {
        Dataset.SECURITY,
        Dataset.TRADE_CALENDAR,
        Dataset.CLASSIFICATION,
    }
    symbol_scoped_datasets = tuple(
        value
        for value in datasets
        if value not in global_datasets
        and not (mode_value is TushareMode.VIP and value in VIP_API_NAMES)
    )
    if not symbols and symbol_scoped_datasets:
        _echo("stock-scoped datasets require at least one --symbol", err=True)
        raise typer.Exit(code=2)

    token = ""
    engine = None
    try:
        token = configured_tushare_token()
        settings = Settings.from_env()
        if not settings.database_url:
            _echo("backfill requires DANG_DATABASE_URL", err=True)
            raise typer.Exit(code=3)
        if settings.tushare_token is None:
            _echo("backfill requires DANG_TUSHARE_TOKEN", err=True)
            raise typer.Exit(code=3)

        token = settings.tushare_token.get_secret_value()
        engine, factory = create_engine_and_session(settings.database_url)
        requests_per_minute = (
            settings.tushare_vip_requests_per_minute
            if mode_value is TushareMode.VIP
            else settings.tushare_standard_requests_per_minute
        )
        limiter = RequestRateLimiter(requests_per_minute)
        with TushareClient(token, rate_limiter=limiter) as client:
            pipeline = IngestionPipeline(client, factory)
            page_runner = PaginatedPlanRunner(
                lambda page_plan: pipeline.run(page_plan, mode=mode_value.value),
                SqlJobCursorRepository(factory),
                mode=mode_value,
                page_size=settings.tushare_page_size,
            )
            job = BackfillJob(
                page_runner,
                history=SqlBackfillHistory(factory),
                sanitize_error=lambda message: _redact(message, token),
            )
            report = job.run(
                BackfillOptions(parsed_as_of, financial_start=parsed_financial_start),
                mode_value,
                datasets,
                symbols,
            )
        status = "completed"
        if report.failed and report.completed == 0:
            status = "failed"
        elif report.failed or report.rejected:
            status = "partial"
        batches = _batch_results(report.results)
        payload = {
            "status": status,
            "jobRunId": report.job_run_id,
            "planned": report.planned,
            "completed": report.completed,
            "failed": report.failed,
            "rejected": report.rejected,
            "plans": [_plan_payload(result) for result in report.results if isinstance(result, PaginatedResult)],
            "batches": [_batch_payload(batch) for batch in batches],
            "failures": [
                {
                    "dataset": failure.dataset.value,
                    "requestKey": failure.request_key,
                    "errorCode": failure.error_code,
                    "message": _redact(failure.message, token),
                    "retryable": failure.retryable,
                }
                for failure in report.failures
            ],
        }
        if publish and status == "completed" and set(datasets).intersection({Dataset.INCOME, Dataset.BALANCE_SHEET,
                Dataset.CASHFLOW, Dataset.FINANCIAL_INDICATOR, Dataset.DIVIDEND, Dataset.DAILY, Dataset.DAILY_BASIC, Dataset.SHAREHOLDER_COUNT}):
            from app.stocks.marts.metric_cards import rebuild_metric_cards
            # VIP 财报按报告期回传全市场行，CLI 的 --symbol 无法限制实际写入范围。
            period_wide = mode_value is TushareMode.VIP and any(dataset in VIP_API_NAMES for dataset in datasets)
            payload["metricCards"] = rebuild_metric_cards(factory, dry_run=False, symbols=() if period_wide else symbols, shareholder_returns=True)
            if payload["metricCards"]["status"] not in {"completed", "no_change"}:
                payload["status"] = "partial"
        _echo(json.dumps(payload, ensure_ascii=False))
        if report.failed or report.rejected or payload["status"] == "partial":
            raise typer.Exit(code=4)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(
            json.dumps(
                {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "message": _redact(str(exc), token),
                },
                ensure_ascii=False,
            ),
            err=True,
        )
        raise typer.Exit(code=3) from exc
    finally:
        if engine is not None:
            engine.dispose()


@app.command("build-and-publish")
def build_and_publish(
    as_of: str = typer.Option(..., help="YYYY-MM-DD"),
    symbol: list[str] | None = typer.Option(None),
) -> None:
    try:
        parsed_as_of = date.fromisoformat(as_of)
    except ValueError:
        _echo("--as-of 必须是 YYYY-MM-DD", err=True)
        raise typer.Exit(code=2) from None
    try:
        symbols = tuple(dict.fromkeys(normalize_stock_symbol(value) for value in (symbol or ())))
    except Exception as exc:
        _echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    engine = None
    database_url = ""
    try:
        settings = Settings.from_env()
        if not settings.database_url:
            _echo("build-and-publish requires DANG_DATABASE_URL", err=True)
            raise typer.Exit(code=3)
        database_url = settings.database_url
        engine, factory = create_engine_and_session(database_url)
        job = BuildAndPublishJob(
            repository=CoreSnapshotRepository(factory),
            derived=DerivedPipeline(factory),
            builders=DefaultMartBuilders(factory, settings=settings),
            quality=QualityGate(),
            publisher=SnapshotPublisher(factory),
            history=SqlBuildHistory(factory),
            sanitize_error=lambda message: _redact_sensitive(message, database_url),
        )
        lock = job_lock_for_engine(engine)
        key = data_publication_lock_key()
        if not lock.try_acquire(key):
            _echo("publication locked", err=True)
            raise typer.Exit(code=5)
        try:
            report = job.run(BuildAndPublishOptions(parsed_as_of, symbols))
        finally:
            lock.release(key)
        payload = {
            "status": report.status,
            "jobRunId": report.job_run_id,
            "asOf": report.as_of.isoformat(),
            "version": report.version,
            "releaseVersion": report.release_version,
            "promotedProducts": list(report.promoted_products),
            "currentVersions": dict(report.current_versions),
            "symbols": list(report.symbols),
            "metricCount": report.metric_count,
            "riskCount": report.risk_count,
            "products": list(report.products),
            "quality": [
                {
                    "name": result.name,
                    "severity": result.severity,
                    "passed": result.passed,
                    "details": result.details,
                }
                for result in report.quality
            ],
            "missingData": list(report.missing_data),
        }
        if report.error:
            payload["error"] = report.error
        _echo(json.dumps(payload, ensure_ascii=False))
        if report.status != "completed":
            raise typer.Exit(code=4)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(
            json.dumps(
                {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "message": _redact_sensitive(str(exc), database_url),
                },
                ensure_ascii=False,
            ),
            err=True,
        )
        raise typer.Exit(code=4) from exc
    finally:
        if engine is not None:
            engine.dispose()

@app.command("sync-daily")
def sync_daily(
    business_date: str = typer.Option(..., help="YYYY-MM-DD"),
    universe: str | None = typer.Option(
        None,
        help="股票池：representative-50、custom 或 all",
    ),
    universe_file: Path | None = typer.Option(
        None,
        help="custom 股票代码 JSON 文件",
    ),
    symbol: list[str] | None = typer.Option(
        None,
        help="本次关注股票，必须属于当前股票池",
    ),
    force: bool = typer.Option(False, help="显式重跑已完成日期"),
    confirm_full_market: bool = typer.Option(False, "--confirm-full-market", help="本次明确授权全市场同步"),
) -> None:
    try:
        parsed_business_date = date.fromisoformat(business_date)
    except ValueError:
        _echo("--business-date 必须是 YYYY-MM-DD", err=True)
        raise typer.Exit(code=2) from None
    if universe is not None and universe not in {"representative-50", "custom", "all"}:
        _echo(
            "--universe 必须是 representative-50、custom 或 all",
            err=True,
        )
        raise typer.Exit(code=2)
    if universe is not None and universe != "custom" and universe_file is not None:
        _echo("--universe-file is only valid for custom universe", err=True)
        raise typer.Exit(code=2)
    try:
        symbols = tuple(
            dict.fromkeys(normalize_stock_symbol(value) for value in (symbol or ()))
        )
    except Exception as exc:
        _echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    token = configured_tushare_token()
    database_url = ""
    try:
        settings = Settings.from_env()
        profile, selected_file = resolve_cli_selection(
            settings, profile=universe, universe_file=universe_file,
            confirm_full_market=confirm_full_market,
        )
        database_url = settings.database_url or ""
        if settings.tushare_token is not None:
            token = settings.tushare_token.get_secret_value()
        with open_daily_sync_runtime(settings, profile, selected_file) as runtime:
            outside = sorted(set(symbols) - set(runtime.selection.symbols))
            if outside:
                _echo(
                    f"symbol {outside[0]} is outside universe "
                    f"{runtime.selection.profile}",
                    err=True,
                )
                raise typer.Exit(code=2)
            report = runtime.job.run(
                DailySyncOptions(
                    business_date=parsed_business_date,
                    mode=runtime.mode,
                    symbols=symbols,
                    lookback_days=settings.sync_disclosure_lookback_days,
                    force=force,
                )
            )
        _echo(json.dumps(report.to_payload(), ensure_ascii=False))
        if report.status == "locked":
            raise typer.Exit(code=5)
        if report.status == "failed":
            raise typer.Exit(code=4)
    except UniverseConfigError as exc:
        _echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except DailySyncRuntimeError as exc:
        _echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(
            json.dumps(
                {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "message": _redact_sensitive(
                        str(exc),
                        token,
                        database_url,
                    ),
                },
                ensure_ascii=False,
            ),
            err=True,
        )
        raise typer.Exit(code=3) from exc


@app.command("retry-failures")
def retry_failures(
    job_run_id: str | None = typer.Option(None),
    publish: bool = typer.Option(False, "--publish", help="重试完成后检查当前工作台积压的八卡输入"),
    dataset: list[str] | None = typer.Option(None),
    error_code: str | None = typer.Option(None),
) -> None:
    try:
        datasets = tuple(Dataset(value) for value in (dataset or ()))
    except ValueError as exc:
        _echo(f"unsupported dataset: {exc.args[0]}", err=True)
        raise typer.Exit(code=2) from None

    token = ""
    engine = None
    try:
        token = configured_tushare_token()
        settings = Settings.from_env()
        if not settings.database_url:
            _echo("retry-failures requires DANG_DATABASE_URL", err=True)
            raise typer.Exit(code=3)
        if settings.tushare_token is not None:
            token = settings.tushare_token.get_secret_value()
        engine, factory = create_engine_and_session(settings.database_url)
        runners: dict[TushareMode, PaginatedPlanRunner] = {}
        with ExitStack() as clients:
            def require_token() -> None:
                if settings.tushare_token is None:
                    _echo("retry-failures requires DANG_TUSHARE_TOKEN", err=True)
                    raise typer.Exit(code=3)

            def run_plan(plan, mode: TushareMode):
                page_runner = runners.get(mode)
                if page_runner is None:
                    requests_per_minute = (
                        settings.tushare_vip_requests_per_minute
                        if mode is TushareMode.VIP
                        else settings.tushare_standard_requests_per_minute
                    )
                    client = clients.enter_context(
                        TushareClient(
                            token,
                            rate_limiter=RequestRateLimiter(requests_per_minute),
                        )
                    )
                    pipeline = IngestionPipeline(client, factory)
                    page_runner = PaginatedPlanRunner(
                        lambda page_plan: pipeline.run(page_plan, mode=mode.value),
                        SqlJobCursorRepository(factory),
                        mode=mode,
                    )
                    runners[mode] = page_runner
                return page_runner(plan)

            report = RetryFailuresJob(
                run_plan,
                SqlFailureRepository(factory),
                sanitize_error=lambda message: _redact(message, token),
                before_retry=require_token,
            ).run(
                FailureQuery(
                    job_run_id=job_run_id,
                    datasets=datasets,
                    error_code=error_code,
                )
            )

        results = tuple(
            outcome.result
            for outcome in report.outcomes
            if outcome.result is not None
        )
        batches = _batch_results(results)
        status = "completed"
        if report.failed or report.skipped:
            status = "partial" if report.completed else "failed"
        payload = {
            "status": status,
            "selected": report.selected,
            "attempted": report.attempted,
            "completed": report.completed,
            "failed": report.failed,
            "skipped": report.skipped,
            "outcomes": [
                _retry_outcome_payload(outcome, token)
                for outcome in report.outcomes
            ],
            "plans": [
                _plan_payload(result)
                for result in results
                if isinstance(result, PaginatedResult)
            ],
            "batches": [_batch_payload(batch) for batch in batches],
        }
        if publish and status == "completed":
            from app.stocks.marts.metric_cards import rebuild_metric_cards
            payload["metricCards"] = rebuild_metric_cards(factory, dry_run=False, shareholder_returns=True)
            if payload["metricCards"]["status"] not in {"completed", "no_change"}:
                payload["status"] = "partial"
        _echo(json.dumps(payload, ensure_ascii=False))
        if report.failed or report.skipped or payload["status"] == "partial":
            raise typer.Exit(code=4)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(
            json.dumps(
                {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "message": _redact(str(exc), token),
                },
                ensure_ascii=False,
            ),
            err=True,
        )
        raise typer.Exit(code=3) from exc
    finally:
        if engine is not None:
            engine.dispose()


@app.command("smoke-tushare")
def smoke_tushare() -> None:
    settings = Settings.from_env()
    if settings.tushare_token is None:
        _echo("Tushare Token 未配置，跳过真实 smoke test", err=True)
        raise typer.Exit(code=2)

    today = date.today()
    plan = build_request_plans(
        Dataset.TRADE_CALENDAR,
        TushareMode.STANDARD,
        SyncScope(start_date=today, end_date=today),
    )[0]
    token = settings.tushare_token.get_secret_value()
    try:
        with TushareClient(token) as client:
            response = client.call(plan.api_name, plan.params, plan.fields)
    except TushareError as exc:
        message = str(exc).replace(token, "***")
        _echo(
            json.dumps(
                {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "message": message,
                },
                ensure_ascii=False,
            ),
            err=True,
        )
        raise typer.Exit(code=3) from exc

    _echo(
        json.dumps(
            {"status": "ok", "api": plan.api_name, "rows": len(response.items)},
            ensure_ascii=False,
        )
    )


def _macro_secrets(settings: Settings | None) -> tuple[str, ...]:
    if settings is None:
        return ()
    database_password = ""
    if settings.database_url:
        try:
            database_password = make_url(settings.database_url).password or ""
        except Exception:
            pass
    return (
        settings.database_url or "",
        database_password,
        settings.fred_api_key.get_secret_value() if settings.fred_api_key else "",
        settings.tushare_token.get_secret_value() if settings.tushare_token else "",
    )


def _macro_as_of(day: date | None, timezone: str) -> datetime:
    local = ZoneInfo(timezone)
    if day is None:
        return datetime.now(local)
    return datetime.combine(day, time.max, tzinfo=local)


@app.command('catchup-macro')
def catchup_macro() -> None:
    try:
        report = run_macro_catchup(Settings.from_env())
        _echo(json.dumps(report, ensure_ascii=False))
        if report['status'] == 'locked':
            raise typer.Exit(code=5)
        if report['status'] == 'blocked':
            raise typer.Exit(code=3)
        if report['status'] in ('failed', 'partial') and not report['activeCoverageCompleted']:
            raise typer.Exit(code=4)
    except typer.Exit:
        raise
    except Exception:
        _echo(json.dumps({'error': {'code': 'MACRO_CATCHUP_UNAVAILABLE'}}))
        raise typer.Exit(code=3) from None


@app.command('plan-macro-catchup')
def plan_macro_catchup() -> None:
    """只读核对最近到期周、补采窗口和安全边界；不执行补跑。"""
    try:
        report = read_macro_catchup_plan(Settings.from_env())
        _echo(json.dumps(report, ensure_ascii=False))
    except Exception:
        # 配置验证/数据库异常也可能携带秘密；不输出异常正文或URL。
        _echo(json.dumps({'status':'failed','error':{'code':'MACRO_CATCHUP_PLAN_UNAVAILABLE'}}))
        raise typer.Exit(code=3) from None


@app.command("sync-macro")
def sync_macro(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD"),
    force: bool = typer.Option(False),
) -> None:
    parsed: date | None = None
    if as_of is not None:
        try:
            parsed = date.fromisoformat(as_of)
            if parsed.isoformat() != as_of:
                raise ValueError("non-canonical date")
        except ValueError:
            _echo("--as-of must be YYYY-MM-DD", err=True)
            raise typer.Exit(code=2) from None
    settings: Settings | None = None
    try:
        settings = Settings.from_env()
        if not settings.database_url:
            raise MacroSyncRuntimeError("sync-macro requires DANG_DATABASE_URL")
        MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        with open_macro_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(
                as_of=_macro_as_of(parsed, settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version,
                catalog_hash=runtime.catalog.catalog_hash,
                force=force, trigger="cli",
                mode="replay" if parsed is not None else "live",
            )
            _echo(json.dumps({
                "preflight": "sync-macro", "asOf": options.as_of.isoformat(),
                "mode": options.mode, "catalogVersion": options.catalog_version,
            }, ensure_ascii=False), err=True)
            report: MacroSyncReport = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False, default=str))
        if report.status in ("partial", "failed"):
            raise typer.Exit(code=4)
        if report.status == "locked":
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(json.dumps({
            "status": "failed", "error": {"code": "MACRO_SETUP_UNAVAILABLE"},
        }, ensure_ascii=False))
        _echo(_redact_sensitive(f"{type(exc).__name__}: {exc}", *_macro_secrets(settings)), err=True)
        raise typer.Exit(code=3) from None


def _macro_history_error_code(exc: Exception, fallback: str) -> str:
    # 只允许内部定义的完整稳定码；不公开异常文本或按前缀取码，
    # 避免连接凭据、配置值或伪装成错误码的外部输入泄露。
    if isinstance(exc, ValueError) and len(exc.args) == 1 and isinstance(exc.args[0], str):
        code = exc.args[0]
        if code in {
            'NBS_HISTORY_DATABASE_REQUIRED', 'NBS_HISTORY_CHECKPOINT_INVALID',
            'NBS_HISTORY_CONFIG_INVALID', 'NBS_HISTORY_CONFIG_MISMATCH',
            'NBS_HISTORY_MANIFEST_INVALID', 'NBS_HISTORY_MANIFEST_CONFLICT',
            'NBS_HISTORY_RECONCILE_EVIDENCE_INVALID', 'NBS_HISTORY_RECONCILE_INCOMPLETE',
            'MACRO_CATALOG_VERSION_HASH_MISMATCH',
            'MACRO_HISTORY_CATALOG_UNAVAILABLE',
            'GOVBOND_HISTORY_DATABASE_REQUIRED', 'GOVBOND_HISTORY_CHECKPOINT_INVALID',
            'GOVBOND_HISTORY_CONFIG_INVALID', 'GOVBOND_HISTORY_CONFIG_MISMATCH',
            'GOVBOND_HISTORY_SOURCE_INVALID', 'GOVBOND_HISTORY_CLOCK_INVALID',
            'GOVBOND_HISTORY_INCOMPLETE',
            'DR007_HISTORY_DATABASE_REQUIRED', 'DR007_HISTORY_TOKEN_REQUIRED',
            'DR007_HISTORY_CHECKPOINT_INVALID', 'DR007_HISTORY_CONFIG_INVALID',
            'DR007_HISTORY_CONFIG_MISMATCH', 'DR007_HISTORY_SOURCE_INVALID',
            'DR007_HISTORY_CLOCK_INVALID',
            'DR007_HISTORY_INCOMPLETE',
            'USDCNY_HISTORY_DATABASE_REQUIRED', 'USDCNY_HISTORY_CHECKPOINT_INVALID',
            'USDCNY_HISTORY_CONFIG_INVALID', 'USDCNY_HISTORY_CONFIG_MISMATCH',
            'USDCNY_HISTORY_SOURCE_INVALID', 'USDCNY_HISTORY_CLOCK_INVALID',
            'US_RATES_DATABASE_REQUIRED', 'US_RATES_KEY_REQUIRED', 'US_RATES_CONFIG_INVALID',
            'US_RATES_SOURCE_INVALID', 'US_RATES_CLOCK_INVALID', 'US_RATES_CORE_REQUIRED',
            'NBS_SYNC_DATABASE_REQUIRED', 'NBS_SYNC_CONFIG_INVALID', 'NBS_SYNC_SOURCE_INVALID',
            'NBS_SYNC_CLOCK_INVALID', 'NBS_SYNC_CORE_REQUIRED',
            'TUSHARE_MACRO_DATABASE_REQUIRED', 'TUSHARE_MACRO_TOKEN_REQUIRED',
            'TUSHARE_MACRO_CONFIG_INVALID', 'TUSHARE_MACRO_SOURCE_INVALID',
            'TUSHARE_MACRO_CLOCK_INVALID', 'TUSHARE_MACRO_CORE_REQUIRED',
            'FRED_MACRO_DATABASE_REQUIRED', 'FRED_MACRO_KEY_REQUIRED', 'FRED_MACRO_CONFIG_INVALID',
            'FRED_MACRO_SOURCE_INVALID', 'FRED_MACRO_CLOCK_INVALID', 'FRED_MACRO_CORE_REQUIRED',
        }:
            return code
    return fallback


@app.command('sync-fred-macro')
def sync_fred_macro() -> None:
    """六项已初始化 FRED 宏观指标有界增量，保留 BLS 回退、独立审计、不自动发布。"""
    try:
        settings = Settings.from_env()
        with open_fred_macro_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version, catalog_hash=runtime.catalog.catalog_hash,
                force=True, trigger='cli', mode='live', series_ids=FRED_MACRO_SERIES_IDS, publish=False)
            report = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False, default=str))
        if report.status in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report.status == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'FRED_MACRO_SYNC_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command('sync-tushare-macro')
def sync_tushare_macro() -> None:
    """六项已初始化 Tushare 宏观指标增量，独立审计、不自动发布。"""
    try:
        settings = Settings.from_env()
        with open_tushare_macro_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version, catalog_hash=runtime.catalog.catalog_hash,
                force=True, trigger='cli', mode='live', series_ids=TUSHARE_MACRO_SERIES_IDS, publish=False)
            report = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False, default=str))
        if report.status in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report.status == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'TUSHARE_MACRO_SYNC_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command('sync-nbs')
def sync_nbs() -> None:
    """两项已初始化NBS月频增量，根配置、独立审计、不自动发布。"""
    try:
        settings = Settings.from_env()
        with open_nbs_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version, catalog_hash=runtime.catalog.catalog_hash,
                force=True, trigger='cli', mode='live', series_ids=NBS_SERIES_IDS, publish=False)
            report = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False, default=str))
        if report.status in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report.status == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'NBS_SYNC_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command('sync-us-rates')
def sync_us_rates() -> None:
    """两项已初始化美国利率的FRED有界增量，根配置、独立审计、不自动发布。"""
    try:
        settings = Settings.from_env()
        with open_us_rates_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version, catalog_hash=runtime.catalog.catalog_hash,
                force=True, trigger='cli', mode='live', series_ids=US_RATE_SERIES_IDS, publish=False)
            report = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False, default=str))
        if report.status in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report.status == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'US_RATES_SYNC_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command('sync-govbond')
def sync_govbond() -> None:
    """已完成历史的国债当前增量；只读根配置，不自动发布。"""
    try:
        settings = Settings.from_env()
        with open_govbond_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version, catalog_hash=runtime.catalog.catalog_hash,
                force=True, trigger='cli', mode='live', series_ids=('cn.rate.govbond_10y',), publish=False)
            report = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(), ensure_ascii=False, default=str))
        if report.status in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report.status == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'GOVBOND_SYNC_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command('sync-dr007')
def sync_dr007() -> None:
    """已核验历史的DR007当前增量；根配置，不自动发布。"""
    try:
        settings = Settings.from_env()
        with open_dr007_sync_runtime(settings) as runtime:
            options = MacroSyncOptions(as_of=_macro_as_of(None,settings.sync_timezone),
                history_years=settings.macro_history_years,
                catalog_version=runtime.catalog.catalog_version,catalog_hash=runtime.catalog.catalog_hash,
                force=True,trigger='cli',mode='live',series_ids=('cn.rate.dr007',),publish=False)
            report = runtime.job.run(options)
        _echo(json.dumps(report.to_payload(),ensure_ascii=False,default=str))
        if report.status in ('partial','failed'):
            raise typer.Exit(code=4)
        if report.status=='locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc,'DR007_SYNC_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status':'failed','error':{'code':code}},ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("backfill-macro-history")
def backfill_macro_history() -> None:
    """按根 .env 配置执行一批 NBS 历史任务，自动恢复已保存进度。"""
    settings: Settings | None = None
    try:
        settings = Settings.from_env()
        with open_macro_history_runtime(settings) as runtime:
            report = runtime.job.run(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years, end=settings.macro_backfill_end,
                request_limit=settings.macro_backfill_request_limit, releases=runtime.releases)
        _echo(json.dumps(report, ensure_ascii=False, default=str))
        if report['status'] in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report['status'] == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        # checkpoint/数据库错误可能含连接凭据；只公开稳定码，不回显异常输入。
        code = _macro_history_error_code(exc, 'NBS_HISTORY_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("backfill-govbond-history")
def backfill_govbond_history() -> None:
    """执行一批配置驱动的国债历史回填；不登记来源、不发布快照。"""
    try:
        settings = Settings.from_env()
        with open_govbond_history_runtime(settings) as runtime:
            report = runtime.job.run(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years, end=settings.macro_backfill_end,
                request_limit=settings.macro_backfill_request_limit)
        _echo(json.dumps(report, ensure_ascii=False, default=str))
        if report['status'] in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report['status'] == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'GOVBOND_HISTORY_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("govbond-history-status")
def govbond_history_status_command() -> None:
    """只读核对国债回填进度与 raw/core 证据，不创建 HTTP 客户端。"""
    try:
        report = read_govbond_history_status(Settings.from_env())
        _echo(json.dumps(report, ensure_ascii=False, default=str))
    except Exception as exc:
        code = _macro_history_error_code(exc, 'GOVBOND_HISTORY_STATUS_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("backfill-usdcny-history")
def backfill_usdcny_history() -> None:
    """配置驱动的一批USD/CNY中间价历史；不登记来源或发布。"""
    try:
        settings = Settings.from_env()
        with open_usdcny_history_runtime(settings) as runtime:
            report = runtime.job.run(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years, end=settings.macro_backfill_end,
                request_limit=settings.macro_backfill_request_limit)
        _echo(json.dumps(report, ensure_ascii=False, default=str))
        if report['status'] in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report['status'] == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'USDCNY_HISTORY_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("usdcny-history-status")
def usdcny_history_status_command() -> None:
    """只读核验USD/CNY回填raw/core，不创建HTTP或写游标。"""
    try:
        report = read_usdcny_history_status(Settings.from_env())
        _echo(json.dumps(report, ensure_ascii=False, default=str))
    except Exception as exc:
        code = _macro_history_error_code(exc, 'USDCNY_HISTORY_STATUS_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("backfill-dr007-history")
def backfill_dr007_history() -> None:
    """执行一批配置驱动的DR007历史回填；不登记来源、不发布快照。"""
    try:
        settings = Settings.from_env()
        with open_dr007_history_runtime(settings) as runtime:
            report = runtime.job.run(as_of=_macro_as_of(None, settings.sync_timezone),
                history_years=settings.macro_history_years, end=settings.macro_backfill_end,
                request_limit=settings.macro_backfill_request_limit)
        _echo(json.dumps(report, ensure_ascii=False, default=str))
        if report['status'] in ('partial', 'failed'):
            raise typer.Exit(code=4)
        if report['status'] == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'DR007_HISTORY_SETUP_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("dr007-history-status")
def dr007_history_status_command() -> None:
    """只读核对DR007回填进度与 raw/core 证据，不创建 HTTP 客户端。"""
    try:
        report = read_dr007_history_status(Settings.from_env())
        _echo(json.dumps(report, ensure_ascii=False, default=str))
    except Exception as exc:
        code = _macro_history_error_code(exc, 'DR007_HISTORY_STATUS_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None

@app.command("reconcile-macro-history")
def reconcile_macro_history_command() -> None:
    """零网络核对已完成NBS回填并更新当前缺口；不发布快照。"""
    try:
        settings = Settings.from_env()
        report = reconcile_macro_history(settings, _macro_as_of(None, settings.sync_timezone))
        _echo(json.dumps(report, ensure_ascii=False, default=str))
        if report['status'] == 'locked':
            raise typer.Exit(code=5)
    except typer.Exit:
        raise
    except Exception as exc:
        code = _macro_history_error_code(exc, 'NBS_HISTORY_RECONCILE_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("macro-history-status")
def macro_history_status_command() -> None:
    """只读查看 NBS 历史游标和缺月，不发 HTTP 或发布快照。"""
    try:
        report = read_macro_history_status(Settings.from_env())
        _echo(json.dumps(report, ensure_ascii=False, default=str))
    except Exception as exc:
        code = _macro_history_error_code(exc, 'NBS_HISTORY_STATUS_UNAVAILABLE')
        _echo(json.dumps({'status': 'failed', 'error': {'code': code}}, ensure_ascii=False))
        raise typer.Exit(code=3) from None


@app.command("smoke-macro")
def smoke_macro(provider: str = typer.Option(..., help="fred 或 tushare")) -> None:
    if provider not in ("fred", "tushare"):
        _echo("--provider must be fred or tushare", err=True)
        raise typer.Exit(code=2)
    settings: Settings | None = None
    try:
        settings = Settings.from_env()
        catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        pairs = tuple(
            (series, source) for series in catalog.series for source in series.sources
            if source.provider == provider
        )
        pair = next((item for item in pairs if item[0].frequency == "daily"), None)
        if pair is None:
            pair = pairs[0] if pairs else None
        if pair is None:
            raise MacroSyncRuntimeError("provider has no catalog series")
        series, source = pair
        now = datetime.now(ZoneInfo(settings.sync_timezone))
        end = now.date()
        start = (
            end - timedelta(days=6) if series.frequency == "daily" else
            end.replace(day=1) if series.frequency == "monthly" else
            end.replace(month=((end.month - 1) // 3) * 3 + 1, day=1)
        )
        window = MacroFetchWindow(start, end, now)
        with ExitStack() as resources:
            if provider == "fred":
                key = settings.fred_api_key.get_secret_value() if settings.fred_api_key else ""
                if not key:
                    raise ProviderAuthError("FRED_KEY_MISSING")
                client = resources.enter_context(httpx.Client(timeout=catalog.defaults.request_timeout_seconds))
                http = resources.enter_context(MacroHttpTransport(
                    client=client, max_attempts=catalog.defaults.max_attempts,
                    backoff_seconds=catalog.defaults.retry_backoff_seconds,
                ))
                selected = CalendarFredMacroProvider(key, transport=http)
            else:
                token = settings.tushare_token.get_secret_value() if settings.tushare_token else ""
                if not token:
                    raise ProviderAuthError("TUSHARE_TOKEN_MISSING")
                client = resources.enter_context(TushareClient(token))
                selected = TushareMacroProvider(client)
            result = selected.fetch(series, source, window)
        _echo(json.dumps({
            "provider": provider, "series": series.id, "status": result.status,
            "count": len(result.observations),
        }, ensure_ascii=False))
        if result.status == "unavailable" or isinstance(
            result.error, (ProviderAuthError, ProviderPermissionError)
        ):
            raise typer.Exit(code=3)
        if result.status == "failed":
            raise typer.Exit(code=4)
    except typer.Exit:
        raise
    except ProviderAuthError as exc:
        _echo(exc.code, err=True)
        raise typer.Exit(code=3) from None
    except Exception as exc:
        _echo(_redact_sensitive(f"{type(exc).__name__}: {exc}", *_macro_secrets(settings)), err=True)
        raise typer.Exit(code=3 if settings is None or isinstance(exc, MacroSyncRuntimeError) else 4) from None


def _external_importer() -> ExternalImporter:
    settings = Settings.from_env()
    if not settings.database_url:
        _echo("外部导入需要配置 DANG_DATABASE_URL", err=True)
        raise typer.Exit(code=3)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return ExternalImporter(sessionmaker(engine, expire_on_commit=False), ExternalFileReader(settings.external_import_path))


@app.command("validate-import")
def validate_import(path: Path) -> None:
    """只校验外部 JSON/JSONL，不写数据库。"""
    try:
        settings = Settings.from_env()
        preview = ExternalImporter(sessionmaker(create_engine("sqlite://")), ExternalFileReader(settings.external_import_path)).validate(path)
    except (OSError, ValueError) as exc:
        _echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _echo(json.dumps({"fileHash": preview.file_hash, "rowCount": preview.row_count, "macroCount": preview.macro_count, "contentCount": preview.content_count, "errors": preview.errors}, ensure_ascii=False, default=str))
    if preview.errors:
        raise typer.Exit(code=2)


@app.command("import-jsonl")
def import_jsonl(path: Path) -> None:
    """将已校验的外部 JSON/JSONL 导入数据库。"""
    try:
        summary = _external_importer().import_file(path)
    except ImportValidationFailed as exc:
        _echo(json.dumps({"status": "rejected", "errors": exc.errors}, ensure_ascii=False), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _echo("数据库导入失败", err=True)
        raise typer.Exit(code=3) from exc
    _echo(json.dumps({"status": summary.status, "fileId": summary.file_id, "fileHash": summary.file_hash, "rows": summary.row_count, "affectedProducts": summary.affected_products}, ensure_ascii=False))


def _macro_publish_payload(report: MacroPublishReport) -> dict[str, object]:
    return {
        "status": report.status,
        "version": report.version,
        "products": list(report.products),
        "promotedProducts": list(report.promoted_products),
        "quality": [
            {"name": item.name, "severity": item.severity,
             "passed": item.passed, "details": item.details}
            for item in report.quality
        ],
    }


from app.jobs.run_errors import JobLockedError


class MacroRebuildLockedError(MacroSyncRuntimeError, JobLockedError):
    pass


def _macro_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _macro_rebuild_outcomes(
    catalog: MacroSeriesCatalog, observations: tuple[MacroObservation, ...],
    current: Mapping[str, MacroSeriesStateView], as_of: datetime,
) -> tuple[MacroSeriesOutcome, ...]:
    visible_rows = select_visible_observations(
        observations, as_of,
        {
            series.id: tuple(
                source.provider for source in sorted(series.sources, key=lambda item: item.priority)
            ) for series in catalog.series
        },
    )
    latest: dict[str, MacroObservation] = {}
    for row in visible_rows:
        prior = latest.get(row.series_id)
        if prior is None or row.observed_at > prior.observed_at:
            latest[row.series_id] = row
    outcomes: list[MacroSeriesOutcome] = []
    for series in catalog.series:
        visible = latest.get(series.id)
        state = current.get(series.id)
        if state is not None and state.last_checked_at is not None and _macro_utc(state.last_checked_at) > as_of:
            state = None
        source = visible.source if visible else None
        newer_than_state = (
            visible is not None and state is not None and (
                state.last_checked_at is None
                or state.latest_observed_at is None
                or visible.observed_at > state.latest_observed_at
                or _macro_utc(visible.released_at) > _macro_utc(state.last_checked_at)
            )
        )
        expired = visible is not None and observation_expired(series, visible.observed_at, as_of.date())
        state_reason = state.last_error_code if state is not None and state.stale_reason == "missing" else getattr(state, "stale_reason", None)
        # 新观测可恢复来源失败，但不能证明整个明确历史缺口已经补齐。
        source_failed = state is not None and state.stale and (not newer_than_state or state_reason == "MACRO_HISTORY_GAP")
        status: MacroSeriesStatus = "missing" if visible is None else (
            "stale" if expired or source_failed else
            "fallback" if series.sources and source != series.sources[0].provider else "fresh"
        )
        outcomes.append(MacroSeriesOutcome(
            series.id, status, source, 0, 0, 0, (), status == "fallback",
            stale_reason=state_reason if source_failed else "MACRO_VALUE_EXPIRED" if expired else None,
        ))
    return tuple(outcomes)


def _run_macro_rebuild(settings: Settings, *, reuse_unchanged: bool = True) -> MacroPublishReport:
    if not settings.database_url:
        raise MacroSyncRuntimeError("macro rebuild requires DANG_DATABASE_URL")
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    engine, factory = create_engine_and_session(settings.database_url)
    try:
        as_of = datetime.now(ZoneInfo(settings.sync_timezone))
        lock = job_lock_for_engine(engine)
        key = data_publication_lock_key()
        if not lock.try_acquire(key):
            raise MacroRebuildLockedError("macro publication is locked")
        try:
            states = MacroSeriesStateRepository(factory, catalog.catalog_version)
            state_views = states.get_all()
            from app.macro.enterprise_contract import enterprise_loader
            from app.macro.extensions.view import load_extension_inputs
            builder = MacroMartBuilder(MacroRepository(factory), catalog=catalog,
                                       enterprise_loader=enterprise_loader(factory, settings),
                                       extension_loader=lambda cutoff: load_extension_inputs(factory, cutoff))
            observations = builder.load_observations(catalog, as_of)
            outcomes = _macro_rebuild_outcomes(
                catalog, observations, {item.series_id: item for item in state_views}, as_of,
            )
            history = SqlMacroSyncHistory(factory)
            business_key = f"rebuild-macro:{as_of.isoformat()}"
            run_id = history.start(
                business_key, as_of, catalog.catalog_version,
                catalog.catalog_hash, "cli", 1,
            )
            try:
                report = MacroPublishJob(
                    builder, SnapshotPublisher(factory), history,
                ).publish(
                    run_id, as_of, catalog, outcomes,
                    observations=observations, state_views=state_views,
                    **({'reuse_unchanged': True} if reuse_unchanged else {}),
                )
                history.finish(run_id, "completed" if not report.blocked else "failed", {
                    "publicationVersion": report.version,
                    "products": list(report.products),
                })
                return report
            except Exception:
                history.finish(run_id, "failed", {"errorCode": "MACRO_PUBLICATION_FAILED"})
                raise
        finally:
            lock.release(key)
    finally:
        engine.dispose()


@app.command("rebuild-external-marts")
def rebuild_external_marts(product: str = typer.Option(..., help="macro 或 content")) -> None:
    if product not in {"macro", "content"}:
        _echo("--product 必须是 macro 或 content", err=True)
        raise typer.Exit(code=2)
    if product == "content":
        try:
            content_settings = Settings.from_env()
        except Exception:
            _echo(json.dumps({"status": "blocked", "code": "CONTENT_CONFIGURATION_UNAVAILABLE"}))
            raise typer.Exit(code=3) from None
        content_report = rebuild_content_marts(content_settings)
        _echo(json.dumps(content_report, ensure_ascii=False, default=str))
        if content_report["status"] in {"completed", "no_change"}:
            return
        content_code = content_report.get("code")
        code = 5 if content_code == "CONTENT_LOCK_BUSY" else (
            3 if content_code in {"CONTENT_DATABASE_REQUIRED", "CONTENT_NO_BASE_SNAPSHOT"} else 4)
        raise typer.Exit(code=code)
    settings: Settings | None = None
    try:
        settings = Settings.from_env()
        report = _run_macro_rebuild(settings)
        _echo(json.dumps(_macro_publish_payload(report), ensure_ascii=False, default=str))
        if report.blocked:
            raise typer.Exit(code=4)
    except typer.Exit:
        raise
    except Exception as exc:
        _echo(_redact_sensitive(f"{type(exc).__name__}: {exc}", *_macro_secrets(settings)), err=True)
        code = (
            5 if isinstance(exc, MacroRebuildLockedError) else
            3 if settings is None or isinstance(exc, MacroSyncRuntimeError) else 4
        )
        raise typer.Exit(code=code) from None


from app.cli.backup import register_backup_commands
from app.cli.release import register_release_commands
from app.cli.capacity import register_capacity_commands
from app.cli.classification import register_classification_commands
from app.cli.stock_update import register_stock_update_commands
from app.cli.routine_update import register_routine_commands
from app.cli.market_rebuild import register_market_rebuild_commands
from app.cli.market_data import register_market_data_commands
from app.cli.market_pulse import register_market_pulse_commands
from app.cli.security_region import register_security_region_commands
from app.cli.etf_heatmap import register_etf_heatmap_commands
from app.cli.commodities import register_commodity_commands
from app.cli.macro_enterprise import register_macro_enterprise_commands
from app.cli.macro_extensions import register_macro_extension_commands
from app.cli.official_members import register_official_member_commands

register_backup_commands(app)
register_release_commands(app)
register_capacity_commands(app)
register_classification_commands(app)
register_stock_update_commands(app)
register_routine_commands(app, macro_rebuild=lambda settings: _run_macro_rebuild(settings, reuse_unchanged=True))
from app.cli.financial_supply import register_financial_supply
register_financial_supply(app)
from app.cli.valuation_supply import register_valuation_supply
register_valuation_supply(app)
from app.cli.business_supply import register_business_supply
register_business_supply(app)
register_market_rebuild_commands(app)
from app.cli.stock_research import register_stock_research_commands
register_stock_research_commands(app)
from app.cli.portfolio_rebuild import register_portfolio_rebuild_commands
register_portfolio_rebuild_commands(app)
register_market_data_commands(app)
register_market_pulse_commands(app)
register_security_region_commands(app)
register_etf_heatmap_commands(app)
register_commodity_commands(app)
register_macro_enterprise_commands(app)
register_macro_extension_commands(app)
register_official_member_commands(app)
from app.cli.news import register_news_commands
register_news_commands(app)
from app.cli.announcement_history import register_announcement_commands
register_announcement_commands(app)

install_cli_observability(app, safety_critical_commands=frozenset({
    "build-and-publish", "rebuild-market-marts", "sync-market-data", "sync-market-pulse", "sync-security-region", "sync-etf-heatmap", "sync-commodities", "sync-daily", "backfill", "backfill-universe", "retry-failures",
    "rebuild-external-marts", "sync-macro", "sync-fred-macro", "sync-nbs",
    "sync-tushare-macro", "sync-us-rates", "sync-govbond", "sync-dr007",
    "catchup-macro", "backup-database", "verify-database-backup", "verify-release", "sync-classification", "sync-stock-data", "update-routine",
}))

if __name__ == "__main__":
    app()
