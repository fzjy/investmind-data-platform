from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import app.config as config_module
import app.cli.main as cli_module
import app.jobs.scheduler as scheduler_module
import app.bootstrap as bootstrap_module
from app.stocks.jobs.daily_sync import DailySyncReport
from app.connectors.tushare.strategies import TushareMode
from app.stocks.universes import UniverseResolver
from app.config import Settings


def root_config(monkeypatch, tmp_path, *, profile="custom", extra=""):
    pool = tmp_path / "pool.json"
    pool.write_text('["600519.SH"]', encoding="utf8")
    config = tmp_path / ".env"
    config.write_text(
        "DANG_DATA_BACKEND=database\nDANG_DATABASE_URL=sqlite+pysqlite:///:memory:\n"
        "DANG_TUSHARE_TOKEN=controlled-token\nDANG_TUSHARE_MODE=vip\n"
        "DANG_SYNC_DISCLOSURE_LOOKBACK_DAYS=11\n"
        f"DANG_SYNC_UNIVERSE={profile}\n"
        + (f"DANG_SYNC_UNIVERSE_FILE={pool.as_posix()}\n" if profile == "custom" else "")
        + extra, encoding="utf8",
    )
    monkeypatch.setattr(config_module, "PROJECT_ENV_FILE", config)
    return pool


def controlled_daily(monkeypatch):
    events = []

    @contextmanager
    def open_runtime(settings, profile, universe_file):
        events.append(("open", profile, universe_file))
        selection = UniverseResolver(active_symbols=lambda: ("600519.SH",)).resolve(profile, universe_file=universe_file)

        class Job:
            def run(self, options):
                assert options.mode is TushareMode.VIP
                assert options.lookback_days == 11
                return DailySyncReport(
                    status="completed", job_run_id="controlled-run", business_date=options.business_date,
                    mode=options.mode, universe=selection, request_count=0, provider_count=0,
                    retained_count=0, filtered_count=0, row_count=0,
                )

        try:
            yield SimpleNamespace(job=Job(), selection=selection, mode=TushareMode(settings.tushare_mode))
        finally:
            events.append("closed")

    monkeypatch.setattr(cli_module, "open_daily_sync_runtime", open_runtime)
    return events


def test_daily_default_uses_root_custom_pool_and_same_root_mode(monkeypatch, tmp_path):
    pool = root_config(monkeypatch, tmp_path)
    events = controlled_daily(monkeypatch)
    result = CliRunner().invoke(cli_module.app, ["sync-daily", "--business-date", "2026-10-13"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["universe"]["profile"] == "custom"
    assert payload["universe"]["size"] == 1
    assert events == [("open", "custom", pool), "closed"]


def test_daily_explicit_profile_does_not_carry_old_custom_file(monkeypatch, tmp_path):
    root_config(monkeypatch, tmp_path)
    events = controlled_daily(monkeypatch)
    result = CliRunner().invoke(cli_module.app, ["sync-daily", "--business-date", "2026-10-13", "--universe", "representative-50"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["universe"]["profile"] == "representative-50"
    assert events == [("open", "representative-50", None), "closed"]


def test_daily_explicit_custom_file_overrides_configured_file(monkeypatch, tmp_path):
    root_config(monkeypatch, tmp_path)
    explicit = tmp_path / "other.json"
    explicit.write_text('["000001.SZ"]', encoding="utf8")
    events = controlled_daily(monkeypatch)
    result = CliRunner().invoke(cli_module.app, ["sync-daily", "--business-date", "2026-10-13", "--universe-file", str(explicit)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["universe"]["profile"] == "custom"
    assert events == [("open", "custom", explicit), "closed"]


@pytest.mark.parametrize("arguments", [[], ["--universe", "all"]])
def test_daily_all_without_confirmation_never_opens_runtime(monkeypatch, tmp_path, arguments):
    root_config(monkeypatch, tmp_path, profile="all")
    events = controlled_daily(monkeypatch)
    result = CliRunner().invoke(cli_module.app, ["sync-daily", "--business-date", "2026-10-13", *arguments])
    assert result.exit_code == 2
    assert "--confirm-full-market" in result.output
    assert events == []


def test_daily_all_confirmation_runs_only_controlled_selection(monkeypatch, tmp_path):
    root_config(monkeypatch, tmp_path, profile="all")
    events = controlled_daily(monkeypatch)
    result = CliRunner().invoke(cli_module.app, ["sync-daily", "--business-date", "2026-10-13", "--confirm-full-market"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["universe"]["profile"] == "all"
    assert events == [("open", "all", None), "closed"]


def test_daily_confirmation_rejected_for_non_all(monkeypatch, tmp_path):
    root_config(monkeypatch, tmp_path)
    events = controlled_daily(monkeypatch)
    result = CliRunner().invoke(cli_module.app, ["sync-daily", "--business-date", "2026-10-13", "--confirm-full-market"])
    assert result.exit_code == 2
    assert events == []


def test_scheduler_all_requires_confirmation_before_runtime(monkeypatch, tmp_path, capsys):
    root_config(monkeypatch, tmp_path, profile="all")
    opened = []

    def forbidden(*args):
        opened.append("runtime")
        raise RuntimeError("must not enter")

    monkeypatch.setattr(scheduler_module, "open_daily_sync_runtime", forbidden)
    assert scheduler_module.main() == 1
    assert opened == []
    assert "--confirm-full-market" in capsys.readouterr().err


def test_scheduler_confirmed_all_uses_root_pool_without_real_scheduler(monkeypatch, tmp_path):
    root_config(monkeypatch, tmp_path, profile="all")
    events = []

    @contextmanager
    def runtime(settings, profile, universe_file):
        events.append(("open", profile, universe_file))
        try:
            yield object()
        finally:
            events.append("closed")

    monkeypatch.setattr(scheduler_module, "open_daily_sync_runtime", runtime)
    monkeypatch.setattr(scheduler_module, "build_scheduler_runtime", lambda *args: SimpleNamespace(run=lambda: events.append("run")))
    assert scheduler_module.main(["--confirm-full-market"]) == 0
    assert events == [("open", "all", None), "run", "closed"]


@pytest.mark.parametrize("module", [scheduler_module, bootstrap_module])
def test_module_help_returns_before_settings(monkeypatch, module, capsys):
    def forbidden():
        pytest.fail("help must not read configuration or start a runtime")

    monkeypatch.setattr(module.Settings, "from_env", forbidden)
    with pytest.raises(SystemExit) as exit:
        module.main(["--help"])
    assert exit.value.code == 0
    assert "--help" in capsys.readouterr().out


@pytest.mark.parametrize("module", [scheduler_module, bootstrap_module])
def test_module_unknown_argument_does_not_echo_secret_or_read_settings(monkeypatch, module, capsys):
    monkeypatch.setattr(module.Settings, "from_env", lambda: pytest.fail("unknown args must not read configuration"))
    with pytest.raises(SystemExit) as exit:
        module.main(["--invalid", "secret-sentinel-url-password"])
    assert exit.value.code == 2
    output = capsys.readouterr()
    assert "secret-sentinel" not in output.err + output.out
    assert "--help" in output.err


def test_bootstrap_force_configuration_alone_never_opens_database(monkeypatch, tmp_path, capsys):
    root_config(monkeypatch, tmp_path, extra="DANG_BOOTSTRAP_FORCE=true\n")
    opened = []

    def forbidden(*args):
        opened.append("database")
        raise RuntimeError("unexpected database access")

    monkeypatch.setattr(bootstrap_module, "create_engine_and_session", forbidden)
    assert bootstrap_module.main() == 1
    assert opened == []
    error = json.loads(capsys.readouterr().err)
    assert "--force" in error["message"]


@pytest.mark.parametrize("configured_force", [False, True])
def test_explicit_bootstrap_force_reaches_runner_and_closes_resources(monkeypatch, tmp_path, configured_force, capsys):
    root_config(monkeypatch, tmp_path, extra=f"DANG_BOOTSTRAP_FORCE={str(configured_force).lower()}\n")
    events = []
    engine = SimpleNamespace(dispose=lambda: events.append("disposed"))
    monkeypatch.setattr(bootstrap_module, "create_engine_and_session", lambda *args: (engine, object()))

    def run(self, options):
        assert options.force is True
        assert options.mode == "vip"
        assert options.symbols == ("600519.SH",)
        events.append("run")
        return bootstrap_module.BootstrapReport("completed", "controlled-version", options.symbols, options.as_of)

    monkeypatch.setattr(bootstrap_module.BootstrapRunner, "run", run)
    assert bootstrap_module.main(["--force"]) == 0
    assert events == ["run", "disposed"]
    assert json.loads(capsys.readouterr().out)["version"] == "controlled-version"


@pytest.mark.parametrize("command", [
    ["sync-daily", "--business-date", "2026-10-13"], ["backfill-universe"], ["backup-database"],
])
@pytest.mark.parametrize("extra", [["secret-sentinel-review"], ["--secret-sentinel-review"], ["--unknown", "secret-sentinel-review"]])
def test_typer_task_parameter_errors_never_echo_secret_or_read_settings(monkeypatch, command, extra):
    monkeypatch.setattr(cli_module.Settings, "from_env", lambda: pytest.fail("invalid arguments must not open configuration/runtime"))
    result = CliRunner().invoke(cli_module.app, [*command, *extra])
    assert result.exit_code == 2
    assert "secret-sentinel" not in result.stdout + result.stderr
    assert "--help" in result.stdout + result.stderr


@pytest.mark.parametrize("args", [["secret-sentinel-review"], ["--secret-sentinel-review"]])
def test_typer_root_parameter_error_is_sanitized(monkeypatch, args):
    monkeypatch.setattr(cli_module.Settings, "from_env", lambda: pytest.fail("root argument error must not read configuration"))
    result = CliRunner().invoke(cli_module.app, args)
    assert result.exit_code == 2
    assert "secret-sentinel" not in result.stdout + result.stderr


@pytest.mark.parametrize(("module", "flag"), [(bootstrap_module, "--for"), (scheduler_module, "--confirm")])
def test_dangerous_confirmation_requires_complete_spelling(monkeypatch, module, flag, capsys):
    def forbidden():
        raise RuntimeError("configuration must not load for incomplete confirmation")

    monkeypatch.setattr(module.Settings, "from_env", forbidden)
    with pytest.raises(SystemExit) as exit:
        module.main([flag])
    assert exit.value.code == 2
    assert "--help" in capsys.readouterr().err
