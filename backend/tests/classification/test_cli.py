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
from backend.tests.derived.test_inputs import _factory


def cli(monkeypatch, *, backend="database"):
    app = typer.Typer()
    classification.register_classification_commands(app)
    factory = _factory()
    engine = factory.kw["bind"]
    settings = Settings(_env_file=None, data_backend=backend, database_url="sqlite+pysqlite:///:memory:",
        tushare_token=SecretStr("secret-test-token"))
    monkeypatch.setattr(classification.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(classification, "create_engine_and_session", lambda url: (engine, factory))
    mapping = policy()
    monkeypatch.setattr(classification.StockMappingPolicy, "load", lambda: mapping)
    # Keep the in-memory test DB available for an independent read after CLI closes its resources.
    monkeypatch.setattr(engine, "dispose", lambda: None)
    return app, factory


def test_cli_dry_run_reads_config_without_job_or_provider(monkeypatch):
    app, factory = cli(monkeypatch)
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
