import json
from collections import Counter
from datetime import date

import httpx
import typer

from app.config import Settings
from app.connectors.cninfo import CninfoAnnouncements
from app.content.announcement_history import Window, history_plan, preview_pages
from app.shared.readonly_database import open_readonly_factory
from app.stocks.jobs.daily_sync_repository import DailySyncRepository
from app.stocks.universes import UniverseResolver


def register_announcement_commands(app):
    @app.command("announcement-history-sample", help="仅同步指定池内单股、最多 31 天的公告，可发布该工作台。")
    def announcement_history_sample(symbol: str = typer.Option(...), start: str = typer.Option(...),
                                    end: str = typer.Option(...), max_pages: int = typer.Option(1, min=1, max=3),
                                    publish: bool = False):
        from app.content.announcement_runtime import run_announcements
        window = Window(symbol.upper(), date.fromisoformat(start), date.fromisoformat(end))
        if not 0 <= (window.end - window.start).days < 31:
            raise typer.BadParameter("样本范围须为 1 至 31 天")
        result = run_announcements(Settings.from_env(), page_budget=max_pages, window=window, publish=publish)
        typer.echo(json.dumps(result, ensure_ascii=False))
        if result["status"] != "completed" or result.get("publication", {}).get("status") == "locked":
            raise typer.Exit(code=4)

    @app.command("announcement-history-preview", help="只读抽查一只沪深证券一个月以内的巨潮公告。")
    def announcement_history_preview(
        symbol: str = typer.Option(...), start: str = typer.Option(...), end: str = typer.Option(...),
        page_size: int = typer.Option(10, min=1, max=30), max_pages: int = typer.Option(1, min=1, max=3),
    ):
        try:
            window = Window(symbol.upper(), date.fromisoformat(start), date.fromisoformat(end))
            if not 0 <= (window.end - window.start).days < 31:
                raise ValueError("抽查日期范围须为 1 至 31 天")
            with httpx.Client(timeout=15) as client:
                source = CninfoAnnouncements(client, page_size)
                result = preview_pages(window, source.fetch_page, max_pages)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
            typer.echo(f"巨潮样本查询失败：{exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(json.dumps(result, ensure_ascii=False))

    @app.command("announcement-history-status", help="按配置股票池预览巨潮公告回填范围、覆盖天数及下一窗口。")
    def announcement_history_status():
        settings = Settings.from_env()
        if not settings.database_read_url:
            typer.echo("未配置读取数据库")
            raise typer.Exit(code=3)
        with open_readonly_factory(settings.database_read_url) as factory:
            selection = UniverseResolver(active_symbols=DailySyncRepository(factory).active_symbols).resolve(
                settings.sync_universe, universe_file=settings.sync_universe_file)
            with factory() as session:
                pending, coverage, waiting = history_plan(session, selection.symbols)
        typer.echo(json.dumps({
            "source": "cninfo", "profile": selection.profile, "stockCount": len(selection.symbols),
            "pendingWindows": len(pending), "windowStates": dict(Counter(status for _, status in pending)),
            "nextWindow": pending[0][0].key if pending else None, "waitingForCandles": waiting,
            "coverage": coverage,
        }, ensure_ascii=False))
