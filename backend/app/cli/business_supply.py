"""主营构成有界补采；默认只读预检。"""
import typer
from app.config import Settings
from app.ops.observability import emit_report
from app.stocks.business_supply import run_business_step


def register_business_supply(app):
    @app.command('sync-business-segments',help='按当前已发布证券范围补采产品/地区构成；默认只读，--execute续采，--publish完成后发布。')
    def sync(dry_run:bool=typer.Option(False,'--dry-run'),execute:bool=typer.Option(False,'--execute'),publish:bool=typer.Option(False,'--publish')):
        try:result=run_business_step(Settings.from_env(),dry_run=dry_run or not (execute or publish),publish_requested=publish)
        except Exception:result=dict(status='blocked',code='BUSINESS_SUPPLY_UNAVAILABLE',requestCount=0)
        emit_report(result)
        raise typer.Exit(0 if result['status'] in ('planned','completed','not_selected') else 4)
