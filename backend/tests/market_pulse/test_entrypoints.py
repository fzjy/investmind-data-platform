import importlib
import json
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.blocking import BlockingScheduler
from typer.testing import CliRunner

from app.config import Settings
from app.cli.main import app
from app.jobs.scheduler import configure_scheduler
from app.market.data.catalog import MarketDataCatalog
from backend.tests.market_pulse.test_repository import NOW
from backend.tests.jobs.test_scheduler import _runtime


def test_cli_default_is_readonly_and_publish_is_explicit(monkeypatch):
    assert importlib.util.find_spec('app.cli.market_pulse') is not None, 'pulse CLI missing'
    module = importlib.import_module('app.cli.market_pulse')
    calls = []
    def run(settings, *, now, dry_run, publish):
        calls.append((dry_run, publish))
        return {'status': 'planned' if dry_run else 'completed', 'requestCount': 0}
    monkeypatch.setattr(module, 'run_pulse_update', run)
    monkeypatch.setattr(Settings, 'from_env', lambda: Settings())
    runner = CliRunner()
    for arguments in ([], ['--publish'], ['--dry-run', '--publish']):
        assert runner.invoke(app, ['sync-market-pulse', *arguments]).exit_code == 0
    assert calls == [(True, False), (False, True), (True, True)]
    result = runner.invoke(app, ['sync-market-pulse', '--token', 'secret-pass'])
    assert result.exit_code == 2 and 'secret-pass' not in result.output


def test_only_pulse_group_and_actual_http_total(monkeypatch):
    from app.jobs import routine_update as module
    assert 'routine_market_pulse_enabled' in Settings.model_fields, 'pulse flag missing'
    calls = []
    def pulse(settings, **kwargs):
        calls.append(kwargs)
        return {'status': 'completed', 'publishReady': True, 'requestCount': 2, 'publication': {'status': 'no_change'}}
    monkeypatch.setattr(module, 'run_pulse_step', pulse)
    settings = Settings(data_backend='database', database_url='sqlite://', routine_stocks_enabled=False,
        routine_macro_enabled=False, routine_market_pulse_enabled=True)
    result = module.run_routine_update(settings, now=NOW, publish=True, macro_rebuild=lambda _:None)
    assert result['status'] == 'completed' and result['pulse']['requestCount'] == 2
    assert result['httpCounts'] == {'stocks': 0, 'macro': 0, 'pulse': 2, 'total': 2}
    assert calls == [{'now': NOW, 'dry_run': False, 'publish': True}]


def test_pulse_exception_does_not_skip_macro(monkeypatch):
    from app.jobs import routine_update as module
    assert hasattr(module, 'run_pulse_step'), 'pulse routine missing'
    calls = []
    def fail(*args, **kwargs):
        raise RuntimeError('secret-pass')
    monkeypatch.setattr(module, 'run_pulse_step', fail)
    monkeypatch.setattr(module, 'run_macro_catchup', lambda *a, **kw: calls.append('macro') or
        {'status': 'completed', 'activeCoverageCompleted': True, 'requestCount': 0})
    settings = Settings(data_backend='database', database_url='sqlite://', routine_stocks_enabled=False,
        routine_market_pulse_enabled=True)
    result = module.run_routine_update(settings, now=NOW, macro_rebuild=lambda _:None)
    assert result['status'] == 'failed' and result['httpCounts']['total'] is None
    assert calls == ['macro'] and 'secret-pass' not in str(result)


def test_pulse_scheduler_registers_independent_daily_job():
    assert 'routine_market_pulse_enabled' in Settings.model_fields, 'pulse flag missing'
    scheduler = BlockingScheduler(timezone='Asia/Shanghai')
    configure_scheduler(scheduler, Settings(routine_market_pulse_enabled=True),
        SimpleNamespace(run_scheduled=lambda _:None, run_startup=lambda _:None), clock=lambda:NOW,
        pulse_job=lambda:None, pulse_schedule=MarketDataCatalog.load().schedule)
    job = scheduler.get_job('sync-market-pulse')
    assert job.max_instances == 1 and job.coalesce
    assert job.trigger.get_next_fire_time(None, NOW).isoformat() == '2026-10-17T11:00:00+00:00'
    assert scheduler.get_job('sync-market-data') is None


def test_runtime_pulse_uses_lifecycle_guard_and_startup_pacing(monkeypatch):
    runtime = _runtime([])
    assert hasattr(runtime, '_pulse_coordinator'), 'pulse lifecycle missing'
    runtime._settings = runtime._settings.model_copy(update={'routine_market_pulse_enabled': True})
    calls = []
    runtime._pulse_coordinator = SimpleNamespace(catalog=MarketDataCatalog.load(),
        run_scheduled=lambda now:calls.append('scheduled') or {'status': 'completed'},
        run_startup=lambda now:calls.append('startup') or {'status': 'completed'})
    def start(scheduler):
        scheduler.get_job('sync-market-pulse').func()
        assert runtime._active_jobs == 0
        runtime._run_pulse_startup()
        assert runtime._active_jobs == 0
    monkeypatch.setattr(BlockingScheduler, 'start', start)
    runtime.run()
    assert calls == ['scheduled', 'startup']
