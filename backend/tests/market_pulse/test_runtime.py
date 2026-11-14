from backend.tests.support.configuration import assert_fixture_configuration_rejected
from contextlib import contextmanager

from app.config import Settings
from app.market.pulse import runtime
from backend.tests.market_pulse.test_sync import setup, POOL
from backend.tests.market_pulse.test_repository import NOW


def test_readonly_runtime_creates_no_client_engine_or_locks(monkeypatch):
    sync, factory, client, requests = setup()
    @contextmanager
    def readonly(_):
        yield factory
    def forbidden(*args, **kwargs):
        raise AssertionError('must remain readonly')
    monkeypatch.setattr(runtime, 'open_readonly_factory', readonly)
    monkeypatch.setattr(runtime, 'create_engine_and_session', forbidden)
    monkeypatch.setattr(runtime, 'TushareClient', forbidden)
    monkeypatch.setattr(runtime.UniverseResolver, 'resolve', lambda *args: POOL)
    with client:
        result = runtime.run_pulse_update(Settings(data_backend='database', database_url='sqlite://'), now=NOW,
            dry_run=True, publish=True)
    assert result['status'] == 'planned' and result['pendingCount'] == 2 and requests == []


def test_runtime_rejects_fixture_or_non_fixed_pool_before_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('no database access')
    monkeypatch.setattr(runtime, 'open_readonly_factory', forbidden)
    assert_fixture_configuration_rejected(monkeypatch)
    for settings in (Settings(database_url=None, database_read_url=None), Settings(data_backend='database', database_url='sqlite://', sync_universe='all')):
        assert runtime.run_pulse_update(settings, now=NOW, dry_run=True)['status'] == 'configuration_error'
