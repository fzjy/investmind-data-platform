from datetime import UTC, datetime
import json
from typing import Annotated

import typer

from app.config import Settings
from app.market.region.runtime import run_region_update


def register_security_region_commands(app: typer.Typer) -> None:
    @app.command('sync-security-region',help='固定50股当前地域；默认只读，--publish才补齐并发布市场。')
    def sync_security_region(dry_run: Annotated[bool,typer.Option('--dry-run')]=False,
        publish: Annotated[bool,typer.Option('--publish')]=False) -> None:
        try:
            report = run_region_update(Settings.from_env(),now=datetime.now(UTC),dry_run=dry_run or not publish,publish=publish)
        except Exception:
            report = {'status':'configuration_error','code':'REGION_CONFIGURATION_INVALID','requestCount':0}
        typer.echo(json.dumps(report,ensure_ascii=False,default=str))
        code = {'planned':0,'disabled':0,'completed':0,'no_change':0,'configuration_error':2,
            'failed':3,'partial':4,'blocked':4,'locked':5}.get(report['status'],3)
        if code:
            raise typer.Exit(code=code)
