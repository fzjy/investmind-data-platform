from datetime import UTC, datetime
import json
from typing import Annotated

import typer

from app.config import Settings
from app.market.data.runtime import run_market_update


def register_market_data_commands(app: typer.Typer) -> None:
    @app.command('sync-market-data', help='有限六指数/九ETF日线；参数来自配置，默认仅采集，--publish才发布。')
    def sync_market_data(dry_run: Annotated[bool, typer.Option('--dry-run')] = False,
        publish: Annotated[bool, typer.Option('--publish')] = False) -> None:
        try:
            report = run_market_update(Settings.from_env(), now=datetime.now(UTC), dry_run=dry_run, publish=publish)
        except Exception:
            report = {'status': 'configuration_error', 'code': 'MARKET_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(report, ensure_ascii=False, default=str))
        code = {'planned':0, 'disabled':0, 'completed':0, 'no_change':0,
            'configuration_error':2, 'failed':3, 'partial':4, 'blocked':4, 'locked':5}.get(report['status'], 3)
        if code:
            raise typer.Exit(code=code)
