"""当前已发布证券范围的零供应商请求研究投影重建。"""
from contextlib import ExitStack
import json
import typer
from app.config import Settings
from app.db.session import create_engine_and_session
from app.stocks.marts.stock_research_runtime import rebuild_stock_research

def register_stock_research_commands(app):
    @app.command("rebuild-workbench-metric-cards", help="从库内来源重建当前已发布工作台八卡；可重复 --symbol 指定证券。")
    def rebuild_cards(publish: bool = typer.Option(False, "--publish"), dry_run: bool = typer.Option(False, "--dry-run"),
                      symbol: list[str] = typer.Option([], "--symbol")):
        from app.stocks.marts.metric_cards import rebuild_metric_cards
        settings = Settings.from_env()
        if settings.data_backend != "database" or not settings.database_url:
            typer.echo(json.dumps({"status": "blocked", "code": "DATABASE_CONFIGURATION_REQUIRED"}))
            raise typer.Exit(2)
        with ExitStack() as resources:
            engine, factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
            report = rebuild_metric_cards(factory, dry_run=dry_run or not publish, symbols=symbol)
        typer.echo(json.dumps(report, ensure_ascii=False))
        if report["status"] not in {"planned", "completed", "no_change"}:
            raise typer.Exit(3)

    @app.command("rebuild-workbench-shareholder-returns", help="从库内来源重建当前已发布工作台股东回报两个序列；可重复 --symbol 指定证券。")
    def rebuild_returns(publish: bool = typer.Option(False, "--publish"), dry_run: bool = typer.Option(False, "--dry-run"),
                      symbol: list[str] = typer.Option([], "--symbol")):
        from app.stocks.marts.shareholder_returns import rebuild_shareholder_returns
        settings = Settings.from_env()
        if settings.data_backend != "database" or not settings.database_url:
            typer.echo(json.dumps({"status": "blocked", "code": "DATABASE_CONFIGURATION_REQUIRED"}))
            raise typer.Exit(2)
        with ExitStack() as resources:
            engine, factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
            report = rebuild_shareholder_returns(factory, dry_run=dry_run or not publish, symbols=symbol)
        typer.echo(json.dumps(report, ensure_ascii=False))
        if report["status"] not in {"planned", "completed", "no_change"}:
            raise typer.Exit(3)

    @app.command('rebuild-stock-research',help='重建当前已发布股票的研究字段；默认只读，--publish才发布；0供应商HTTP，不改股票池。')
    def rebuild(publish:bool=typer.Option(False,'--publish'),dry_run:bool=typer.Option(False,'--dry-run'),
                business_only:bool=typer.Option(False,'--business-only',help='仅重建当前工作台主营构成')):
        settings=Settings.from_env()
        if settings.data_backend!='database' or not settings.database_url:
            typer.echo(json.dumps({'status':'blocked','code':'DATABASE_CONFIGURATION_REQUIRED'}));raise typer.Exit(2)
        try:
            with ExitStack() as resources:
                engine,factory=create_engine_and_session(settings.database_url);resources.callback(engine.dispose)
                report=rebuild_stock_research(factory,dry_run=dry_run or not publish,**({"business_only":True} if business_only else {}))
            typer.echo(json.dumps(report,ensure_ascii=False))
            if report['status'] not in {'planned','completed','no_change'}:raise typer.Exit(3)
        except typer.Exit:raise
        except Exception:
            typer.echo(json.dumps({'status':'failed','code':'STOCK_RESEARCH_REBUILD_FAILED'}));raise typer.Exit(3) from None
