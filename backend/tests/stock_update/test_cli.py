import json
import typer
from typer.testing import CliRunner
from sqlalchemy import select, func
from app.config import Settings
from app.db.models import JobCursor, JobRun



def cli(new_pg_factory, monkeypatch):
    from app.cli import stock_update
    app = typer.Typer()
    stock_update.register_stock_update_commands(app)
    f = new_pg_factory()
    engine = f.kw['bind']
    monkeypatch.setattr(stock_update.Settings, 'from_env', lambda: Settings(data_backend='database',
        database_url=engine.url.render_as_string(hide_password=False), stock_update_max_requests_per_run=7))
    return app, f, stock_update


def test_dry_cli_never_creates_job_or_calls_http(new_pg_factory, monkeypatch):
    app, f, module = cli(new_pg_factory, monkeypatch)
    monkeypatch.setattr(module, 'TushareClient', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('HTTP forbidden')))
    result = CliRunner().invoke(app, ['--dry-run'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['maxRequestsPerRun'] == 7
    with f() as s:
        for model in (JobCursor, JobRun):
            assert s.scalar(select(func.count()).select_from(model)) == 0


def test_invalid_cli_does_not_echo_secret(new_pg_factory, monkeypatch):
    app, _, _ = cli(new_pg_factory, monkeypatch)
    result = CliRunner().invoke(app, ['--unknown=secret-token'])
    assert result.exit_code == 2 and 'secret-token' not in result.output
