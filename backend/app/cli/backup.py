from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated

import click
import typer
from typer.core import TyperCommand

from app.config import Settings
from app.ops.backup.contracts import BackupError, BackupResult
from app.ops.backup.service import create_backup, prune_backups, read_backup_status, verify_backup


class SafeBackupCommand(TyperCommand):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError:
            # Click 默认会回显未知位置参数，可能含粘贴的密码/URL。
            raise click.UsageError("无效的备份命令参数；请查看 --help。", ctx) from None


def _execute(stage: str, operation: Callable[[Settings], BackupResult]) -> None:
    started = datetime.now(timezone.utc)
    mode = "docker"
    code = 0
    try:
        settings = Settings.from_env()
        mode = settings.backup_tool_mode
        report = operation(settings)
    except BackupError as error:
        code = error.exit_code
        report = BackupResult(status="busy" if code == 5 else "blocked" if code == 2 else "failed",
            stage=error.stage, reason_code=error.reason_code, tool_mode=mode, started_at=started, ended_at=datetime.now(timezone.utc))
    except ValueError:
        code = 2
        report = BackupResult(status="blocked", stage="configuration", reason_code="invalid_backup_configuration",
            tool_mode=mode, started_at=started, ended_at=datetime.now(timezone.utc))
    except Exception:
        code = 3
        report = BackupResult(status="failed", stage=stage, reason_code="backup_operation_failed",
            tool_mode=mode, started_at=started, ended_at=datetime.now(timezone.utc))
    typer.echo(report.model_dump_json(by_alias=True))
    if code:
        raise typer.Exit(code=code)


def register_backup_commands(app: typer.Typer) -> None:
    @app.command("backup-database", cls=SafeBackupCommand, help="到期时只读导出整库；--force 仅绕过周期。")
    def backup_database(force: Annotated[bool, typer.Option("--force")] = False) -> None:
        _execute("backup", lambda settings: create_backup(settings, force=force))

    @app.command("verify-database-backup", cls=SafeBackupCommand, help="只向新建隔离容器恢复并核对，不接受目标 URL。")
    def verify_database_backup(backup_id: Annotated[str | None, typer.Option("--backup-id")] = None) -> None:
        _execute("verification", lambda settings: verify_backup(settings, backup_id=backup_id))

    @app.command("backup-status", cls=SafeBackupCommand, help="只读本地清单，不连接数据库或启动工具。")
    def backup_status() -> None:
        _execute("status", read_backup_status)

    @app.command("prune-database-backups", cls=SafeBackupCommand, help="默认仅列候选；--apply 才定向删除安全备份包。")
    def prune_database_backups(apply: Annotated[bool, typer.Option("--apply")] = False) -> None:
        _execute("prune", lambda settings: prune_backups(settings, apply=apply))
