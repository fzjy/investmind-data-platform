"""组合独立重建入口：默认只读，显式发布。"""
import json

import typer

from app.config import Settings
from app.db.session import create_engine_and_session
from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
from app.portfolio.dividend_report import preview_portfolio_dividends
from app.portfolio.dividend_capture import run_dividend_capture


def register_portfolio_rebuild_commands(app):
    @app.command('capture-portfolio-reference-history',help='批量获取两个已核实全收益指数的当前组合窗口；默认只读，--execute执行，失败不重试、不发布。')
    def reference_history(execute: bool=typer.Option(False,'--execute'),dry_run: bool=typer.Option(False,'--dry-run')):
        from app.portfolio.reference_history import run_history
        settings=Settings.from_env()
        if settings.data_backend!='database' or not settings.database_url:raise typer.Exit(2)
        engine,factory=create_engine_and_session(settings.database_url)
        try:
            report=run_history(settings,factory,execute=execute and not dry_run)
            typer.echo(json.dumps(report,ensure_ascii=False))
            if report['status'] not in ('planned','completed','pending'):raise typer.Exit(3)
        finally:engine.dispose()

    @app.command('capture-portfolio-reference-catalog',help='发现两个全收益指数的供应商目录；默认只读，仅--execute请求，不重试，不发布。')
    def reference_catalog(execute: bool=typer.Option(False,'--execute'),dry_run: bool=typer.Option(False,'--dry-run')):
        from app.portfolio.reference_catalog import run_catalog
        settings=Settings.from_env()
        if settings.data_backend!='database' or not settings.database_url:raise typer.Exit(2)
        engine,factory=create_engine_and_session(settings.database_url)
        try:
            report=run_catalog(settings,factory,execute=execute and not dry_run)
            typer.echo(json.dumps(report,ensure_ascii=False))
            if report['status'] not in ('planned','completed','pending'):raise typer.Exit(3)
        finally:engine.dispose()

    @app.command('capture-portfolio-dividends', help='冻结当前范围补采分红原件；默认只读，--execute采集，--publish同时更新工作台指标卡。')
    def capture(execute: bool = typer.Option(False, '--execute'), dry_run: bool = typer.Option(False, '--dry-run'),
                publish: bool = typer.Option(False, '--publish')):
        settings = Settings.from_env()
        if settings.data_backend != 'database' or not settings.database_url:
            typer.echo(json.dumps({'status':'blocked', 'code':'DATABASE_CONFIGURATION_REQUIRED'}))
            raise typer.Exit(2)
        engine = None
        try:
            engine, factory = create_engine_and_session(settings.database_url)
            report = run_dividend_capture(settings, factory, execute=(execute or publish) and not dry_run)
            if publish and not dry_run and report['status'] == 'completed':
                from app.stocks.marts.metric_cards import rebuild_metric_cards
                report['metricCards'] = rebuild_metric_cards(factory, dry_run=False, shareholder_returns=True)
                if report['metricCards']['status'] not in {'completed', 'no_change'}:
                    report['status'] = 'partial'
            typer.echo(json.dumps(report, ensure_ascii=False))
            if report['status'] not in {'planned', 'completed', 'pending'}:
                raise typer.Exit(3)
        except typer.Exit:
            raise
        except Exception:
            typer.echo(json.dumps({'status':'failed', 'code':'DIVIDEND_CAPTURE_FAILED'}))
            raise typer.Exit(3) from None
        finally:
            if engine is not None:
                engine.dispose()

    @app.command('preview-portfolio-dividends', help='只读当前发布范围的分红/资本字段证据；0HTTP，不采集、不发布。')
    def preview():
        settings = Settings.from_env()
        if settings.data_backend != 'database' or not settings.database_url:
            typer.echo(json.dumps({'status': 'blocked', 'code': 'DATABASE_CONFIGURATION_REQUIRED'}))
            raise typer.Exit(2)
        engine = None
        try:
            engine, factory = create_engine_and_session(settings.database_url)
            report = preview_portfolio_dividends(factory)
            typer.echo(json.dumps(report, ensure_ascii=False))
            if report['status'] == 'blocked':
                raise typer.Exit(3)
        except typer.Exit:
            raise
        except Exception:
            typer.echo(json.dumps({'status': 'failed', 'code': 'DIVIDEND_PREVIEW_FAILED'}))
            raise typer.Exit(3) from None
        finally:
            if engine is not None:
                engine.dispose()

    @app.command('rebuild-portfolio', help='仅用当前股票发布范围的Core原价/因子/上市日期重建组合；默认只读，--publish发布，无供应商HTTP。')
    def rebuild(publish: bool = typer.Option(False, '--publish'), dry_run: bool = typer.Option(False, '--dry-run')):
        settings = Settings.from_env()
        if settings.data_backend != 'database' or not settings.database_url:
            typer.echo(json.dumps({'status': 'blocked', 'code': 'DATABASE_CONFIGURATION_REQUIRED'}))
            raise typer.Exit(2)
        engine = None
        try:
            engine, factory = create_engine_and_session(settings.database_url)
            report = rebuild_portfolio(factory, dry_run=dry_run or not publish)
            typer.echo(json.dumps(report, ensure_ascii=False))
            if report['status'] not in {'planned', 'completed', 'no_change'}:
                raise typer.Exit(3)
        except typer.Exit:
            raise
        except Exception:
            typer.echo(json.dumps({'status': 'failed', 'code': 'PORTFOLIO_REBUILD_FAILED'}))
            raise typer.Exit(3) from None
        finally:
            if engine is not None:
                engine.dispose()
