"""定位源码部署的业务配置，或独立安装包自带的配置。"""
import os
from pathlib import Path


_package_parent = Path(__file__).resolve().parents[1]
_source_layout = _package_parent.name == "backend" and (_package_parent / "alembic.ini").is_file()
_default_root = _package_parent.parent / "config" if _source_layout else _package_parent / "investmind_defaults"
CONFIG_ROOT = Path(os.environ.get("DANG_CONFIG_DIR") or _default_root).resolve()


def resolve_catalog_path(value: Path, relative: str, legacy: str) -> Path:
    """迁移项目自带目录的旧引用，保留外部自定义文件。"""
    if value == Path("config") / relative:
        return CONFIG_ROOT / relative
    if value == Path("backend/config") / legacy or value == _default_root.parent / "backend/config" / legacy:
        return CONFIG_ROOT / relative
    return value
