import json
import typer
from typer.testing import CliRunner


def test_unknown_argument_never_echoes_secret():
    from app.cli.routine_update import register_routine_commands
    app = typer.Typer()
    register_routine_commands(app, macro_rebuild=lambda s: None)
    result = CliRunner().invoke(app, ['--unknown=private-token'])
    assert result.exit_code == 2 and 'private-token' not in result.output


def test_configuration_error_never_echoes_secret(monkeypatch):
    from app.cli import routine_update as m
    app = typer.Typer()
    m.register_routine_commands(app, macro_rebuild=lambda s: None)
    def fail():
        raise ValueError('private-token')
    monkeypatch.setattr(m.Settings, 'from_env', fail)
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 2
    assert json.loads(result.stdout)['errorCode'] == 'ROUTINE_CONFIGURATION_INVALID'
    assert 'private-token' not in result.output
