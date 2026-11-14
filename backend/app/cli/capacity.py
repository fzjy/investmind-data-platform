"""容量命令注册入口；主 CLI 可调用 register_capacity_commands(app)。"""
from __future__ import annotations

from typing import Annotated

import click
import typer
from typer.core import TyperCommand

from app.config import Settings
from app.ops.capacity import CapacityError, CapacityOptions, capacity_benchmark, capacity_status
from app.ops.observability import emit_report


class SafeCapacityCommand(TyperCommand):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError:
            raise click.UsageError("无效的容量参数；请查看 --help。", ctx) from None


def _failure(error: Exception, source: str) -> dict[str, object]:
    if isinstance(error, CapacityError):
        return {"status": "blocked" if error.exit_code == 2 else "failed", "source": source,
                "reasonCode": error.reason_code, "exitCode": error.exit_code}
    from pydantic import ValidationError
    code = 2 if isinstance(error, (ValidationError, ValueError)) else 3
    return {"status": "blocked" if code == 2 else "failed", "source": source,
            "reasonCode": "invalid_capacity_configuration" if code == 2 else "capacity_operation_failed", "exitCode": code}


def _emit(report: dict[str, object]) -> None:
    emit_report(report)
    if report.get("exitCode"):
        raise typer.Exit(code=int(report["exitCode"]))


def register_capacity_commands(app: typer.Typer) -> None:
    @app.command("capacity-status", cls=SafeCapacityCommand, help="显式只读事务统计业务 PG 规模、体积/索引并测选股 API；不采集。")
    def status(timeout_seconds: Annotated[float | None, typer.Option("--timeout-seconds")] = None,
               api_samples: Annotated[int | None, typer.Option("--api-samples")] = None) -> None:
        try:
            settings = Settings.from_env()
            configured = CapacityOptions.from_settings(settings)
            report = capacity_status(settings, timeout_seconds=min(30, configured.timeout_seconds if timeout_seconds is None else timeout_seconds),
                                     api_samples=configured.api_samples if api_samples is None else api_samples)
        except Exception as error:
            report = _failure(error, "business_readonly")
        _emit(report)

    @app.command("capacity-benchmark", cls=SafeCapacityCommand, help="显式新建/清理独立 PostgreSQL17 造数测量；默认 5000×750，需 dev/testcontainers，不接受目标 URL。")
    def benchmark(stocks: Annotated[int | None, typer.Option("--stocks")] = None,
                  days: Annotated[int | None, typer.Option("--days")] = None,
                  batch_size: Annotated[int | None, typer.Option("--batch-size")] = None,
                  api_samples: Annotated[int | None, typer.Option("--api-samples")] = None,
                  timeout_seconds: Annotated[float | None, typer.Option("--timeout-seconds")] = None,
                  build_budget_seconds: Annotated[float | None, typer.Option("--build-budget-seconds")] = None,
                  api_budget_ms: Annotated[float | None, typer.Option("--api-budget-ms")] = None) -> None:
        try:
            settings = Settings.from_env()
            values = CapacityOptions.from_settings(settings).model_dump()
            overrides = {"stock_count": stocks, "trading_days": days, "batch_size": batch_size, "api_samples": api_samples,
                         "timeout_seconds": timeout_seconds, "build_budget_seconds": build_budget_seconds, "api_budget_ms": api_budget_ms}
            values.update({key: value for key, value in overrides.items() if value is not None})
            report = capacity_benchmark(settings, options=CapacityOptions.model_validate(values))
        except Exception as error:
            report = _failure(error, "synthetic")
        _emit(report)


if __name__ == "__main__":
    app = typer.Typer()
    register_capacity_commands(app)
    app()
