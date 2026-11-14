from datetime import UTC, datetime
import json
from typing import Annotated

import typer

from app.config import Settings
from app.market.etf.runtime import run_etf_update


def register_etf_heatmap_commands(app: typer.Typer) -> None:
    @app.command('sync-etf-heatmap',help='固定九ETF自身热力图；默认只读，--publish补齐并发布市场双产品。')
    def sync_etf_heatmap(dry_run: Annotated[bool,typer.Option('--dry-run')]=False,
        publish: Annotated[bool,typer.Option('--publish')]=False) -> None:
        try:
            report=run_etf_update(Settings.from_env(),now=datetime.now(UTC),dry_run=dry_run or not publish,publish=publish)
        except Exception:
            report={'status':'configuration_error','code':'ETF_CONFIGURATION_INVALID','requestCount':0}
        typer.echo(json.dumps(report,ensure_ascii=False,default=str))
        code={'planned':0,'disabled':0,'completed':0,'no_change':0,'configuration_error':2,
            'failed':3,'partial':4,'blocked':4,'locked':5}.get(report['status'],3)
        if code:
            raise typer.Exit(code=code)
