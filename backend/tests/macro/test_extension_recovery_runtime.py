from backend.tests.support.pg_snapshot import database_snapshot
"""显式恢复入口只发冻结计划一次；HTTP边界受控，持久化真实。"""
from datetime import timedelta
import json

import httpx
import pytest
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.macro.jobs.macro_extension_runtime import execute_extension_group
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_recovery import failed, recovery
from backend.tests.macro.test_extension_runtime import enabled
from backend.tests.macro.test_extension_store import database, NOW, count


def test_recovery_sends_frozen_query_after_new_cycle_and_reentry_is_zero(database, tmp_path, monkeypatch):
    db_engine, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    p, parent = failed(factory, tmp_path)
    # failed()写自己的受控配置，再恢复到显式启用文件。
    catalog = enabled(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['params'] == {'ts_code': 'USDCNH.FXCM', 'exchange': 'FXCM', 'start_date': '20260724', 'end_date': '20261021'}
        state = recovery(factory, parent).inspect(p, NOW + timedelta(days=2))
        assert state['status'] == 'uncertain' and count(factory, JobRun) == 2
        return response(200, content=json.dumps({'code': 0, 'data': {**fx(), 'has_more': False}}).encode())
    result = execute_extension_group(factory, catalog, 'usdcnh', 'private-token',
        recovery_of=parent, transport=httpx.MockTransport(handler), now=NOW + timedelta(days=2))
    assert result['status'] == 'completed' and result['supplierHttp'] == 1 and result['insertedRows'] == 1
    before = database_snapshot(db_engine)
    again = execute_extension_group(factory, catalog, 'usdcnh', '', recovery_of=parent,
        transport=httpx.MockTransport(handler), now=NOW + timedelta(days=3))
    assert again['status'] == 'completed' and again['supplierHttp'] == 0 and len(calls) == 1
    assert before == database_snapshot(db_engine)


def test_uncertain_commit_does_not_send_and_later_reentry_does_not_refund(database, tmp_path, monkeypatch):
    _, factory = database
    p, parent = failed(factory, tmp_path)
    catalog = enabled(tmp_path, monkeypatch)
    from app.macro.extensions.recovery import RecoveryStore
    original = RecoveryStore.reserve
    def lost_confirmation(self, plan, now):
        original(self, plan, now)
        raise RuntimeError('private-commit-details')
    monkeypatch.setattr(RecoveryStore, 'reserve', lost_confirmation)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request))
    first = execute_extension_group(factory, catalog, 'usdcnh', 'private-token',
        recovery_of=parent, transport=transport, now=NOW + timedelta(seconds=2))
    assert first['supplierHttp'] == 0 and first['requestCount'] == 1 and first['planPersisted'] is True
    assert first['errorCode'] == 'EXTENSION_RESERVATION_UNCERTAIN'
    again = execute_extension_group(factory, catalog, 'usdcnh', 'private-token',
        recovery_of=parent, transport=transport, now=NOW + timedelta(days=1))
    assert again['supplierHttp'] == 0 and again['errorCode'] == 'EXTENSION_ATTEMPT_UNCERTAIN'
    assert calls == [] and count(factory, JobRun) == 2 and count(factory, SourcePayload) == 0
    assert 'private-commit-details' not in json.dumps(first)


def test_recovery_config_is_rechecked_before_reservation(database, tmp_path, monkeypatch):
    _, factory = database
    _, parent = failed(factory, tmp_path)
    catalog = enabled(tmp_path, monkeypatch)
    def disable():
        path = tmp_path / 'extensions.json'
        value = json.loads(path.read_text())
        value['groups']['usdcnh']['collectionEnabled'] = False
        path.write_text(json.dumps(value))
    calls = []
    result = execute_extension_group(factory, catalog, 'usdcnh', 'private-token',
        recovery_of=parent, before_send=disable, transport=httpx.MockTransport(lambda r: calls.append(r)),
        now=NOW + timedelta(seconds=2))
    assert result['supplierHttp'] == 0 and result['errorCode'] == 'EXTENSION_COLLECTION_CHANGED'
    assert calls == [] and count(factory, JobRun) == 1


def controlled_runtime(factory, monkeypatch):
    from app.macro.jobs import macro_extension_recovery as module
    engine = factory.kw['bind']
    monkeypatch.setattr(module, 'create_engine_and_session', lambda _: (engine, factory))
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    from contextlib import contextmanager
    @contextmanager
    def readonly(_):
        yield factory
    monkeypatch.setattr(module, 'open_readonly_factory', readonly)
    return module, Settings(data_backend='database', database_url='postgresql+psycopg://controlled/isolated', tushare_token='private-token')


def test_recovery_dry_run_is_readonly_and_all_targets_preflight_before_send(database, tmp_path, monkeypatch):
    db_engine, factory = database
    _, parent = failed(factory, tmp_path)
    enabled(tmp_path, monkeypatch)
    module, settings = controlled_runtime(factory, monkeypatch)
    before = database_snapshot(db_engine)
    preview = module.run_macro_extension_recovery(settings, [parent], dry_run=True, now=NOW + timedelta(seconds=2))
    assert preview['supplierHttp'] == 0 and preview['readOnly'] is True and preview['groups'][0]['parentRunId'] == parent
    assert database_snapshot(db_engine) == before
    calls = []
    invalid = '11111111-1111-4111-8111-111111111111'
    with pytest.raises(ExtensionError):
        module.run_macro_extension_recovery(settings, [parent, invalid],
            transport=httpx.MockTransport(lambda r: calls.append(r)), now=NOW + timedelta(seconds=2))
    assert calls == [] and database_snapshot(db_engine) == before


@pytest.mark.parametrize('ids', [[], ['private-token'], ['11111111-1111-4111-8111-111111111111'] * 2,
    ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333']])
def test_invalid_recovery_ids_fail_before_database(ids, monkeypatch):
    from app.macro.jobs import macro_extension_recovery as module
    def forbidden(*args, **kwargs):
        pytest.fail('invalid ids opened database')
    monkeypatch.setattr(module, 'create_engine_and_session', forbidden)
    with pytest.raises(ExtensionError):
        module.run_macro_extension_recovery(Settings(), ids)


def test_cli_requires_explicit_ids_has_no_force_and_redacts_error(monkeypatch):
    result = CliRunner().invoke(app, ['recover-macro-extensions', '--help'])
    assert result.exit_code == 0 and '--failed-run-id' in result.stdout and '--dry-run' in result.stdout
    assert CliRunner().invoke(app, ['recover-macro-extensions']).exit_code == 2
    assert CliRunner().invoke(app, ['recover-macro-extensions', '--force']).exit_code == 2
    result = CliRunner().invoke(app, ['recover-macro-extensions', '--failed-run-id', 'private-token'])
    assert result.exit_code == 2 and 'private-token' not in result.stdout


def test_two_group_recovery_counts_actual_attempts_and_repeat_never_selects_new_period(database, tmp_path, monkeypatch):
    _, factory = database
    _, fx_parent = failed(factory, tmp_path)
    _, gdp_parent = failed(factory, tmp_path, 'nominalGdp')
    enabled(tmp_path, monkeypatch)
    module, settings = controlled_runtime(factory, monkeypatch)
    calls = []
    def handler(request):
        api = json.loads(request.content)['api_name']
        calls.append(api)
        return response(200, content=json.dumps({'code': 0, 'data': fx() if api == 'fx_daily' else gdp()}).encode())
    first = module.run_macro_extension_recovery(settings, [fx_parent, gdp_parent],
        transport=httpx.MockTransport(handler), now=NOW + timedelta(days=2))
    assert first['status'] == 'completed' and first['supplierHttp'] == 2
    assert [g['requestCount'] for g in first['groups']] == [1, 1]
    again = module.run_macro_extension_recovery(settings, [fx_parent, gdp_parent],
        transport=httpx.MockTransport(handler), now=NOW + timedelta(days=9))
    assert again['status'] == 'completed' and again['supplierHttp'] == 0
    assert calls == ['fx_daily', 'cn_gdp'] and count(factory, JobRun) == 4


def test_same_group_two_parents_are_rejected_before_any_new_send(database, tmp_path, monkeypatch):
    from backend.tests.macro.test_extension_store import store
    from backend.tests.macro.test_extension_plan import plan
    _, factory = database
    p, first_parent = failed(factory, tmp_path)
    first = recovery(factory, first_parent)
    key = first.reserve(p, NOW + timedelta(seconds=2))
    first.receive(key, fx(), NOW + timedelta(seconds=3))
    first.replay(key, clock=lambda: NOW + timedelta(seconds=4))
    p2 = plan(tmp_path, at='2026-10-23T20:00:00+08:00')
    second_parent = store(factory).reserve(p2, NOW + timedelta(days=1))
    store(factory).fail(second_parent, 'EXTENSION_RESPONSE_INVALID', NOW + timedelta(days=1, seconds=1))
    enabled(tmp_path, monkeypatch)
    module, settings = controlled_runtime(factory, monkeypatch)
    calls = []
    with pytest.raises(ExtensionError):
        module.run_macro_extension_recovery(settings, [first_parent, second_parent],
            transport=httpx.MockTransport(lambda r: calls.append(r)), now=NOW + timedelta(days=2))
    assert calls == [] and count(factory, JobRun) == 3
