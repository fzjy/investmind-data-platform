import json
import typer
import time
from app.config import Settings
from app.content.news_runtime import run_news


def register_news_commands(app):
    @app.command('news-status', help='只读查看来源与交付状态，不触发采集。')
    def news_status():
        from app.content.routes import read_news
        from app.db.session import create_engine_and_session
        settings = Settings.from_env()
        if not settings.database_url:
            typer.echo('未配置数据库'); raise typer.Exit(code=3)
        engine, factory = create_engine_and_session(settings.database_url)
        try:
            typer.echo(json.dumps(read_news(factory, limit=1)['health'], ensure_ascii=False))
        finally:
            engine.dispose()

    @app.command('sync-news', help='有界采集快讯；--publish交付现有内容库并发布。不会启动常驻调度。')
    def sync_news(dry_run: bool = False, publish: bool = False):
        try:
            report = run_news(Settings.from_env(), publish=publish, dry_run=dry_run)
        except ImportError:
            report = {'status': 'failed', 'code': 'INSTALL_LOCAL_NEWS_PACKAGE', 'requestCount': 0}
        except Exception as exc:
            report = {'status': 'failed', 'code': 'NEWS_CONFIGURATION_INVALID', 'errorType': type(exc).__name__}
        typer.echo(json.dumps(report, ensure_ascii=False, default=str))
        if report['status'] in ('failed', 'delivery_failed', 'blocked', 'locked'):
            raise typer.Exit(code=3)

    @app.command('watch-news', help='显式常驻新闻更新，Ctrl+C停止；按专用配置间隔采集并发布。')
    def watch_news():
        from smartparse_news import Config
        try:
            while True:
                settings = Settings.from_env()
                config = Config.read(settings.news_config_path)
                if not config.enabled:
                    typer.echo('新闻采集已禁用'); return
                report = run_news(settings, publish=True)
                typer.echo(json.dumps(report, ensure_ascii=False, default=str))
                time.sleep(config.interval_seconds)
        except KeyboardInterrupt:
            typer.echo('新闻更新已停止，下次从已保存断点继续。')
