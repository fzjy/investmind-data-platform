from datetime import UTC, datetime
import json
from typing import Annotated

import typer

from app.config import Settings
from app.market.pulse.runtime import run_pulse_update


def register_market_pulse_commands(app: typer.Typer) -> None:
    @app.command('sync-market-pulse', help='固定50股涨跌停；默认只读，--publish才采集并发布。')
    def sync_market_pulse(dry_run: Annotated[bool, typer.Option('--dry-run')] = False,
        publish: Annotated[bool, typer.Option('--publish')] = False) -> None:
        try:
            report = run_pulse_update(Settings.from_env(), now=datetime.now(UTC),
                dry_run=dry_run or not publish, publish=publish)
        except Exception:
            report = {'status': 'configuration_error', 'code': 'PULSE_CONFIGURATION_INVALID', 'requestCount': 0}
        typer.echo(json.dumps(report, ensure_ascii=False, default=str))
        code = {'planned':0, 'disabled':0, 'completed':0, 'no_change':0,
            'configuration_error':2, 'failed':3, 'partial':4, 'blocked':4, 'locked':5}.get(report['status'], 3)
        if code:
            raise typer.Exit(code=code)
