import typer
from app.config import Settings
from app.ops.observability import emit_report
from app.stocks.financial_runtime import run_financial_step


def register_financial_supply(app):
    @app.command('sync-financial-statements',help='按财务配置有界补充当前发布范围的三表原件；默认只读，--execute采集，--publish就绪后发布。')
    def sync(dry_run:bool=typer.Option(False,'--dry-run'),execute:bool=typer.Option(False,'--execute'),publish:bool=typer.Option(False,'--publish')):
        try:
            settings=Settings.from_env()
            if settings.data_backend!='database' or not settings.database_url:raise ValueError('database required')
            result=run_financial_step(settings,dry_run=dry_run or not (execute or publish),publish_requested=publish)
        except Exception:
            result=dict(status='failed',code='FINANCIAL_SUPPLY_UNAVAILABLE',requestCount=None)
        emit_report(result)
        raise typer.Exit(0 if result['status'] in ('planned','completed','no_change','not_selected') else 4)
