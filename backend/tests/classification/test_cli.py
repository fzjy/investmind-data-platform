from backend.tests.support.configuration import assert_fixture_configuration_rejected
import json

from pydantic import SecretStr
import typer
from typer.testing import CliRunner
from sqlalchemy import func, select

from app.cli import classification
from app.config import Settings
from app.db.models import JobCursor, JobRun
from backend.tests.classification.test_sync import policy



def cli(new_pg_factory, monkeypatch, *, backend="database"):
    app = typer.Typer()
    classification.register_classification_commands(app)
    factory = new_pg_factory()
    engine = factory.kw["bind"]
    settings = Settings(_env_file=None, data_backend=backend, database_url=engine.url.render_as_string(hide_password=False),
        tushare_token=SecretStr("secret-test-token"))
    monkeypatch.setattr(classification.Settings, "from_env", lambda: settings)
    mapping = policy()
    monkeypatch.setattr(classification.StockMappingPolicy, "load", lambda: mapping)
    return app, factory


def test_cli_dry_run_reads_config_without_job_or_provider(new_pg_factory, monkeypatch):
    app, factory = cli(new_pg_factory, monkeypatch)
    result = CliRunner().invoke(app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["maxRequestsPerRun"] == 128 and data["minimumRequests"] == 103
    assert data["pool"]["size"] == 50 and data["source"] == "SW2021"
    assert "secret-test-token" not in result.stdout
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRun)) == 0
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 0


def test_fixture_mode_refused_before_connection(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)
