"""由主 CLI 注册的安全发布计划与验收命令。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import click
import typer
from typer.core import TyperCommand

from app.config import Settings
from app.ops.backup.contracts import BackupError
from app.ops.observability import emit_report
from app.ops.release import ReleasePlan, ReleaseReport, release_plan, verify_release


class SafeReleaseCommand(TyperCommand):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError:
            raise click.UsageError("无效的发布参数；请查看 --help。", ctx) from None


def _failure(error: Exception) -> ReleaseReport:
    safe = error if isinstance(error, BackupError) else BackupError("invalid_release_configuration", 2, "configuration")
    now = datetime.now(timezone.utc)
    return ReleaseReport(status="busy" if safe.exit_code == 5 else "blocked" if safe.exit_code == 2 else "failed",
        reason_code=safe.reason_code, exit_code=safe.exit_code, started_at=now, ended_at=now, plan=ReleasePlan())


def _emit(report: ReleasePlan | ReleaseReport) -> None:
    emit_report(report.model_dump(mode="json", by_alias=True))
    if isinstance(report, ReleaseReport) and report.exit_code:
        raise typer.Exit(code=report.exit_code)


def register_release_commands(app: typer.Typer) -> None:
    @app.command("release-plan", cls=SafeReleaseCommand, help="只读本地迁移和可选备份清单，输出生产发布及备份恢复计划。")
    def plan(package: Annotated[str | None, typer.Option("--package")] = None) -> None:
        try:
            report = release_plan(package=package)
        except Exception as error:
            report = _failure(error)
        _emit(report)

    @app.command("verify-release", cls=SafeReleaseCommand, help="仅从明确备份包恢复到自有一次性容器，实际升级并验证 API 与恢复；不接受数据库 URL。")
    def verify(package: Annotated[str, typer.Option("--package")]) -> None:
        try:
            report = verify_release(Settings.from_env(), package=package)
        except Exception as error:
            report = _failure(error)
        _emit(report)
