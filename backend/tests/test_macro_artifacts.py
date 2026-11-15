"""离线验证安装包和 Docker COPY 文件集合，不借用工作区模块。"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROBE = '''
import json, pathlib, sys
sys.path[:] = [p for p in sys.path if "site-packages" not in p]
sys.path.insert(0, sys.argv[1])
sys.path.append(sys.argv[2])
import app.config as config
config.PROJECT_ENV_FILE = pathlib.Path.cwd() / "never-created-user.env"
assert not config.PROJECT_ENV_FILE.exists()
config.dotenv_values = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dotenv access"))
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.shared.stock_mapping import StockMappingPolicy, DEFAULT_STOCK_MAPPING_FILE
from app.config_paths import CONFIG_ROOT
from app.stocks.financial_supply import CONFIG_FILE as financial_config
from app.portfolio.routine import CONFIG_FILE as portfolio_config
assert financial_config.is_relative_to(CONFIG_ROOT) and json.loads(financial_config.read_text(encoding="utf-8"))["bankScoring"]
assert portfolio_config.is_relative_to(CONFIG_ROOT) and json.loads(portfolio_config.read_text(encoding="utf-8"))["routine"]
assert (CONFIG_ROOT / "news/news-collector.json").is_file()
mapping = StockMappingPolicy.load()
assert DEFAULT_STOCK_MAPPING_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
assert mapping.holder_kind("开放式投资基金") == "included"
assert mapping.holder_kind("一般企业") == "excluded"
assert mapping.holder_kind("未知新类型") == "unknown"
catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
assert len(catalog.series) == 20
assert pathlib.Path(config.__file__).is_relative_to(pathlib.Path(sys.argv[3]))
assert DEFAULT_MACRO_CATALOG_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
from app.connectors.macro.nbs_history import DEFAULT_RELEASES_FILE, load_history_releases
releases = load_history_releases(DEFAULT_RELEASES_FILE)
assert DEFAULT_RELEASES_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
assert {r.series_id for r in releases} == {"cn.cpi.core_yoy", "cn.industry.value_added_yoy"}
from app.macro.jobs.govbond_history import DEFAULT_HISTORY_SOURCES_FILE, load_govbond_source
source = load_govbond_source()
assert DEFAULT_HISTORY_SOURCES_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
assert source.provider == "chinamoney" and source.field == "tenRate"
import app.macro.jobs.usdcny_history_runtime as usdcny_runtime
from app.macro.jobs.usdcny_history import load_usdcny_source
usdcny = load_usdcny_source()
assert usdcny.provider == "chinamoney" and usdcny.series == "USD/CNY"
assert usdcny.params == {"maxPages": "4"}
assert catalog.require("cn.fx.usdcny").sources == ()
assert pathlib.Path(usdcny_runtime.__file__).is_relative_to(pathlib.Path(sys.argv[3]))
from app.macro.history_catalog import bind_history_catalog, ARCHIVED_V7_FILE
assert ARCHIVED_V7_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
frozen = bind_history_catalog(catalog, {"catalog_version": "2026-09-30-v7",
    "catalog_hash": "0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a"})
assert frozen.require("cn.rate.govbond_10y").sources == ()
from app.macro.history_catalog import ARCHIVED_V8_FILE
assert ARCHIVED_V8_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
v8 = bind_history_catalog(catalog, {"catalog_version": "2026-10-07-v8",
    "catalog_hash": "5779a4153377655b54f6e2206ad01bcc909d4788421784a8a9016ffc82567713"})
assert v8.require("cn.rate.dr007").sources == ()
from app.market.data.catalog import DEFAULT_MARKET_CATALOG_FILE, MarketDataCatalog
assert DEFAULT_MARKET_CATALOG_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
MarketDataCatalog.load()
from app.stocks.universes.resolver import DEFAULT_REPRESENTATIVE_PATH, UniverseResolver
assert DEFAULT_REPRESENTATIVE_PATH.is_relative_to(pathlib.Path(sys.argv[3]))
assert UniverseResolver().resolve('representative-50').symbols
from app.macro.extensions.config import DEFAULT_EXTENSION_CONFIG_FILE, load_extension_config
assert DEFAULT_EXTENSION_CONFIG_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
load_extension_config(DEFAULT_EXTENSION_CONFIG_FILE)
from app.macro.enterprise_target_catalog import DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE, load_enterprise_target_catalog
assert DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE.is_relative_to(pathlib.Path(sys.argv[3]))
load_enterprise_target_catalog()
print(json.dumps({"module": config.__file__, "catalog": str(DEFAULT_MACRO_CATALOG_FILE), "count": len(catalog.series)}))
'''


def _probe(artifact: Path, cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", PROBE, str(artifact), sysconfig.get_path("purelib"), str(artifact.parent if artifact.name == "backend" else artifact)],
        cwd=cwd, env={"SYSTEMROOT": str(Path("C:/Windows"))},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("kind", ["wheel", "image-copy"])
def test_macro_catalog_loads_from_isolated_artifact(artifact_tmp: Path, kind: str) -> None:
    tmp_path = artifact_tmp
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    cwd = tmp_path / "isolated-cwd"
    cwd.mkdir()
    if kind == "wheel":
        source = tmp_path / "source"
        # 打包源码副本不读取运行缓存；旧 Windows 缓存可能由其他用户创建。
        ignored = shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.egg-info", "build", "tests", ".tmp-*", "backups", "imports", ".env*", "portfolio-evidence", "portfolio-event-resolutions.json")
        def source_ignore(directory, names):
            # 仅根目录data是本机资料，不能误排除app/market/data业务子包。
            return set(ignored(directory, names)) | ({'data'} if Path(directory) == ROOT / 'backend' else set())
        shutil.copytree(ROOT / "backend", source / "backend", ignore=source_ignore)
        shutil.copytree(ROOT / "config", source / "config", ignore=ignored)
        shutil.copy2(ROOT / "pyproject.toml", source)
        built = subprocess.run(
            [sys.executable, "-I", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", "--wheel-dir", str(tmp_path / "wheels"), str(source)],
            cwd=cwd, env={**os.environ, "TMP": str(tmp_path), "TEMP": str(tmp_path)},
            capture_output=True, text=True, check=False,
        )
        assert built.returncode == 0, built.stderr
        with zipfile.ZipFile(next((tmp_path / "wheels").glob("*.whl"))) as wheel:
            assert not any('fixtures/' in name or 'tests/' in name or 'portfolio-evidence/' in name for name in wheel.namelist())
            assert 'app/portfolio/dividend_resolution.py' not in wheel.namelist()
            assert not any('manual_dividend_resolution' in name for name in wheel.namelist())
            for path in (ROOT / "config").rglob("*.json"):
                assert wheel.read("investmind_defaults/" + path.relative_to(ROOT / "config").as_posix()) == path.read_bytes()
            wheel.extractall(artifact)
    else:
        for line in (ROOT / "backend/Dockerfile").read_text().splitlines():
            if not line.startswith("COPY "):
                continue
            *sources, destination = line.split()[1:]
            for value in sources:
                source = ROOT / value
                target = artifact / destination
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target / source.name)
    evidence = _probe(artifact if kind == "wheel" else artifact / "backend", cwd)
    assert evidence["count"] == 20
    print(f"{kind}: {evidence}")


@pytest.fixture
def artifact_tmp():
    # Windows wheel build paths must remain short; own only this new temp child.
    directory = ROOT / ".tmp/artifact-tests"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory, prefix="") as temporary:
        yield Path(temporary)
