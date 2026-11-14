import argparse
from collections.abc import Callable
from typing import NoReturn, TypeVar

import click
import typer.core as typer_core
from typer.core import TyperGroup


# 新版Typer使用自带Click；旧版用外部Click。必须捕获实际解析器的异常族。
_active_click = getattr(typer_core, "_click", click)
_usage_error = _active_click.exceptions.UsageError
_missing_parameter = _active_click.exceptions.MissingParameter
T = TypeVar("T")


def _safe_usage(operation: Callable[[], T], context) -> T:
    message = "无效命令参数；请查看 --help。"
    try:
        return operation()
    except (_usage_error, click.UsageError) as error:
        if isinstance(error, _missing_parameter) and error.param is not None:
            options = [name for name in getattr(error.param, "opts", ()) if name.startswith("--")]
            if options:
                message = f"缺少必要参数 {options[0]}；请查看 --help。"
    # 在except之外抛出，不保留包含原始输入的异常context。
    raise _usage_error(message, ctx=context) from None


class SafeTyperGroup(TyperGroup):
    def parse_args(self, ctx, args: list[str]) -> list[str]:
        parent = super()
        if not args:
            # 无输入的原帮助合同是安全的；不依赖某版本才提供的帮助异常类。
            return parent.parse_args(ctx, args)
        return _safe_usage(lambda: parent.parse_args(ctx, args), ctx)

    def invoke(self, ctx):
        parent = super()
        return _safe_usage(lambda: parent.invoke(ctx), ctx)


class SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        # 不回显未知参数，避免粘贴的Token/URL出现在终端或任务日志。
        super().error("无效参数；请查看 --help。")
