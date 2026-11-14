from collections.abc import Callable
import json
from typing import Annotated

import typer

from app.cli.stock_update import SafeStockUpdateCommand
from app.config import Settings
from app.macro.jobs.macro_sync import MacroPublishReport
from app.jobs.routine_update import run_routine_update, routine_exit_code
from app.ops.observability import emit_report


def register_routine_commands(app: typer.Typer, *, macro_rebuild: Callable[[Settings], MacroPublishReport]) -> None:
    @app.command('update-routine', cls=SafeStockUpdateCommand,
        help='已配置数据组的有界日常运行；--dry-run只读，--publish显式发布；--only-portfolio仅验收组合组。')
    def update_routine(dry_run: Annotated[bool, typer.Option('--dry-run')] = False,
        publish: Annotated[bool, typer.Option('--publish')] = False,
        only_portfolio: Annotated[bool, typer.Option('--only-portfolio')] = False) -> None:
        try:
            settings = Settings.from_env()
            report = run_routine_update(settings, macro_rebuild=macro_rebuild, dry_run=dry_run, publish=publish,
                **({'only_portfolio':True} if only_portfolio else {}))
        except ValueError:
            emit_report({'status': 'blocked', 'errorCode': 'ROUTINE_CONFIGURATION_INVALID'})
            raise typer.Exit(code=2) from None
        except Exception:
            emit_report({'status': 'failed', 'errorCode': 'ROUTINE_UNAVAILABLE'})
            raise typer.Exit(code=3) from None
        emit_report(report)
        code = routine_exit_code(report)
        if code:
            raise typer.Exit(code=code)
