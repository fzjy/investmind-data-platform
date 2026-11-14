"""集中配置、手动执行；dry-run不实例化供应商客户端。"""
from __future__ import annotations

from contextlib import ExitStack
import json
from typing import Annotated

import click
import typer
from typer.core import TyperCommand

from app.stocks.classification.sync import ClassificationSync, RequestBudget
from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.shared.stock_mapping import StockMappingPolicy
from app.stocks.universes import UniverseResolver


class SafeClassificationCommand(TyperCommand):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError:
            raise click.UsageError("无效的分类同步参数；请查看 --help。", ctx) from None


def register_classification_commands(app: typer.Typer) -> None:
    @app.command("sync-classification", cls=SafeClassificationCommand, help="有界同步固定50股SW2021分类，不发布；配置见stock-mapping.json。")
    def sync_classification(dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
        refresh: Annotated[bool, typer.Option("--refresh")] = False) -> None:
        try:
            settings = Settings.from_env()
            if settings.data_backend != "database" or not settings.database_url:
                raise ValueError("分类采集需要database模式和数据库配置")
            if settings.sync_universe != "representative-50" or settings.sync_universe_file is not None:
                raise ValueError("分类采集只接受固定representative-50股票池")
            policy = StockMappingPolicy.load()
            selection = UniverseResolver().resolve("representative-50")
            budget = RequestBudget(policy.classification.sync.maxRequestsPerRun)
            with ExitStack() as resources:
                engine, factory = create_engine_and_session(settings.database_url)
                resources.callback(engine.dispose)
                job = ClassificationSync(factory, None, budget, selection, policy, job_lock_for_engine(engine),
                    market_years=settings.backfill_market_years)
                preflight = job.preflight()
                if dry_run:
                    preflight.update(dryRun=True, refreshRequested=refresh)
                    typer.echo(json.dumps(preflight, ensure_ascii=False, default=str))
                    return
                if settings.tushare_token is None:
                    raise ValueError("Tushare Token未配置")
                rate = (settings.tushare_vip_requests_per_minute if settings.tushare_mode == "vip"
                    else settings.tushare_standard_requests_per_minute)
                job.client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
                    rate_limiter=RequestRateLimiter(rate), before_request=budget.consume))
                result = job.run(refresh=refresh)
                typer.echo(json.dumps(result, ensure_ascii=False, default=str))
                if result["status"] != "completed":
                    raise typer.Exit(code=3)
        except typer.Exit:
            raise
        except ValueError as exc:
            typer.echo(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
            raise typer.Exit(code=2) from None
        except Exception:
            # Database/transport exception representations can include connection credentials.
            typer.echo(json.dumps({"status": "failed", "error": "classification_sync_failed"}))
            raise typer.Exit(code=3) from None
