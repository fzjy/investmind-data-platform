"""估值原件独立入口；默认仅查看计划。"""
import typer
from app.config import Settings
from app.ops.observability import emit_report
from app.stocks.valuation_supply import run_valuation_step


def register_valuation_supply(app):
    @app.command('sync-valuation-history', help='按配置的历史窗口补采已发布选股范围的估值。默认只读，--execute 采集，--publish 完成后发布。')
    def sync(dry_run: bool = typer.Option(False, '--dry-run'), execute: bool = typer.Option(False, '--execute'), publish: bool = typer.Option(False, '--publish')):
        try:
            result = run_valuation_step(Settings.from_env(), dry_run=dry_run or not (execute or publish), publish_requested=publish)
        except Exception:
            result = dict(status='blocked', code='VALUATION_SUPPLY_UNAVAILABLE', requestCount=0)
        emit_report(result)
        raise typer.Exit(0 if result['status'] in ('planned', 'completed', 'not_selected') else 4)
