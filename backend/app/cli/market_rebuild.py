"""市场页面既有股票数据的配置驱动离线重建入口。"""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from typing import Annotated

import typer

from app.config import Settings
from app.db.session import create_engine_and_session
from app.market.marts.market_runtime import rebuild_market_marts
from app.stocks.universes import UniverseResolver


def register_market_rebuild_commands(app: typer.Typer) -> None:
    @app.command("rebuild-market-marts", help="仅重建固定50股市场聚合；默认只读预检，--publish才发布。不采集。")
    def rebuild(dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
                publish: Annotated[bool, typer.Option("--publish")] = False) -> None:
        try:
            settings = Settings.from_env()
            if (settings.data_backend != "database" or not settings.database_url
                    or settings.sync_universe != "representative-50" or settings.sync_universe_file is not None):
                typer.echo(json.dumps({"status": "blocked", "code": "MARKET_CONFIGURATION_REQUIRED"}))
                raise typer.Exit(code=2)
            selection = UniverseResolver().resolve("representative-50")
            with ExitStack() as resources:
                engine, factory = create_engine_and_session(settings.database_url)
                resources.callback(engine.dispose)
                report = rebuild_market_marts(factory, selection, checked_at=datetime.now(timezone.utc),
                    dry_run=dry_run or not publish, timezone_name=settings.sync_timezone,
                    sync_hour=settings.sync_hour, sync_minute=settings.sync_minute,
                    max_classification_drop_ratio=settings.quality_max_row_drop_ratio)
            typer.echo(json.dumps(report, ensure_ascii=False, default=str))
            if report["status"] not in {"planned", "completed", "no_change"}:
                raise typer.Exit(code=5 if report.get("code") == "MARKET_LOCK_BUSY" else 3)
        except typer.Exit:
            raise
        except Exception:
            typer.echo(json.dumps({"status": "failed", "code": "MARKET_REBUILD_FAILED"}))
            raise typer.Exit(code=3) from None
