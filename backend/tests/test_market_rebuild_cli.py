from backend.tests.support.configuration import assert_fixture_configuration_rejected
import importlib

from typer.testing import CliRunner

from app.config import Settings


def test_market_rebuild_rejects_fixture_without_touching_database(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)


def test_market_rebuild_requires_explicit_publish_and_routes_settings(monkeypatch):
    module = importlib.import_module("app.cli.main")
    assert importlib.util.find_spec("app.cli.market_rebuild") is not None, "market CLI missing"
    cli = importlib.import_module("app.cli.market_rebuild")
    settings = Settings(data_backend="database", database_url="sqlite:///controlled", sync_hour=18)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    class Engine:
        def dispose(self):
            pass
    monkeypatch.setattr(cli, "create_engine_and_session", lambda url: (Engine(), "controlled factory"))
    calls = []
    def run(factory, selection, **kwargs):
        assert factory == "controlled factory" and len(selection.symbols) == 50
        assert kwargs["sync_hour"] == 18
        calls.append(kwargs["dry_run"])
        return {"status": "planned" if kwargs["dry_run"] else "completed", "requestCount": 0}
    monkeypatch.setattr(cli, "rebuild_market_marts", run)
    for args in ([], ["--dry-run"], ["--publish"], ["--publish", "--dry-run"]):
        result = CliRunner().invoke(module.app, ["rebuild-market-marts", *args])
        assert result.exit_code == 0, result.output
    assert calls == [True, True, False, True]
