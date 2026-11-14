from datetime import UTC,datetime
import json
from typing import Annotated
import typer
from app.config import Settings
from app.market.commodities.runtime import run_commodity_update


def register_commodity_commands(app:typer.Typer):
    @app.command('sync-commodities',help='固定十国内商品，默认只读；--publish有界补齐真实OHLC并发布市场整对。')
    def sync_commodities(dry_run:Annotated[bool,typer.Option('--dry-run')]=False,publish:Annotated[bool,typer.Option('--publish')]=False):
        try: report=run_commodity_update(Settings.from_env(),now=datetime.now(UTC),dry_run=dry_run or not publish,publish=publish)
        except Exception: report={'status':'configuration_error','code':'COMMODITY_CONFIGURATION_INVALID','requestCount':0}
        typer.echo(json.dumps(report,ensure_ascii=False,default=str))
        code={'planned':0,'disabled':0,'completed':0,'no_change':0,'configuration_error':2,'failed':3,'partial':4,'blocked':4,'locked':5}.get(report['status'],3)
        if code: raise typer.Exit(code=code)
