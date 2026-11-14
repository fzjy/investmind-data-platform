from pathlib import Path

from app.config import Settings
from app.stocks.universes.resolver import UniverseConfigError


def resolve_cli_selection(
    settings: Settings, *, profile: str | None = None,
    universe_file: Path | None = None, confirm_full_market: bool = False,
) -> tuple[str, Path | None]:
    """复用主配置；显式切换profile不携带原池文件，成本确认只来自命令。"""
    selected = profile if profile is not None else settings.sync_universe
    if selected not in {"representative-50", "custom", "all"}:
        raise UniverseConfigError("--universe 必须是 representative-50、custom 或 all")
    configured_file = settings.sync_universe_file if profile is None or selected == settings.sync_universe else None
    selected_file = universe_file if universe_file is not None else configured_file
    if selected == "custom" and selected_file is None:
        raise UniverseConfigError("custom universe requires DANG_SYNC_UNIVERSE_FILE or --universe-file")
    if selected != "custom" and selected_file is not None:
        raise UniverseConfigError("universe file is only valid for custom universe")
    if selected == "all" and not confirm_full_market:
        raise UniverseConfigError("all universe requires --confirm-full-market")
    if selected != "all" and confirm_full_market:
        raise UniverseConfigError("--confirm-full-market is only valid for all universe")
    return selected, selected_file
