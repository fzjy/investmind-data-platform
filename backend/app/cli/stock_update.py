from contextlib import ExitStack
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from typing import Annotated
import click
import typer
from typer.core import TyperCommand
from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.stocks.update.planning import StockUpdateOptions
from app.stocks.update.sync import StockUpdateSync
from app.stocks.update.publication import publish_stock_update
from app.stocks.universes import UniverseResolver


class SafeStockUpdateCommand(TyperCommand):
    def parse_args(self, ctx: click.Context, args: list[str]):
        try:
            return super().parse_args(ctx, args)
        except click.UsageError:
            raise click.UsageError('无效的股票同步参数；请查看 --help。', ctx) from None


def register_stock_update_commands(app):
    @app.command('sync-stock-data', cls=SafeStockUpdateCommand, help='配置驱动、可恢复的固定50股更新；--publish才发布股票快照。')
    def sync_stock_data(dry_run: Annotated[bool, typer.Option('--dry-run')] = False,
        publish: Annotated[bool, typer.Option('--publish')] = False):
        try:
            settings = Settings.from_env()
            if settings.data_backend != 'database' or not settings.database_url:
                raise ValueError('股票更新需要database模式')
            if settings.sync_universe != 'representative-50' or settings.sync_universe_file is not None:
                raise ValueError('股票更新只接受固定representative-50池')
            options = StockUpdateOptions(settings.stock_update_as_of, settings.stock_update_max_requests_per_run,
                settings.stock_update_calendar_lookahead_days, settings.backfill_market_years,
                settings.sync_timezone, settings.sync_hour, settings.sync_minute)
            with ExitStack() as resources:
                engine, factory = create_engine_and_session(settings.database_url)
                resources.callback(engine.dispose)
                lock = job_lock_for_engine(engine)
                selection = UniverseResolver().resolve('representative-50')
                job = StockUpdateSync(factory, None, selection, options, lock)
                if dry_run:
                    typer.echo(json.dumps({**job.preflight(datetime.now(timezone.utc)), 'dryRun': True,
                        'publishRequested': publish}, ensure_ascii=False, default=str))
                    return
                if settings.tushare_token is None:
                    raise ValueError('Tushare Token未配置')
                rate = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
                job.client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(), rate_limiter=RequestRateLimiter(rate)))
                result = job.run(datetime.now(timezone.utc))
                if publish and result['publishReady']:
                    report = publish_stock_update(factory, selection, date.fromisoformat(result['asOf']), lock,
                        collection=result, options=options)
                    result['publication'] = asdict(report)
                typer.echo(json.dumps(result, ensure_ascii=False, default=str))
                if result['status'] != 'completed' or publish and (not result['publishReady'] or result['publication']['status'] != 'completed'):
                    raise typer.Exit(code=3)
        except typer.Exit:
            raise
        except ValueError:
            typer.echo(json.dumps({'status': 'blocked', 'error': 'invalid stock update configuration or readiness; check config and --dry-run'}))
            raise typer.Exit(code=2) from None
        except Exception:
            typer.echo(json.dumps({'status': 'failed', 'error': 'stock_update_failed'}))
            raise typer.Exit(code=3) from None
