"""命令边界及真实SQLite执行组；只替换外部HTTP，不替换持久预算。"""
from datetime import timedelta
import json

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from app.db.models.ops import JobRun
from app.db.models.core import MacroObservation
from app.db.models.raw import SourcePayload
from app.macro.extensions import config as extension_config
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_plan import config, payload, plan
from backend.tests.macro.test_extension_store import database, NOW, count, store


def enabled(tmp_path, monkeypatch):
    raw = payload()
    for value in raw['groups'].values(): value['collectionEnabled'] = True
    result = config(tmp_path, raw)
    monkeypatch.setattr(extension_config, 'DEFAULT_EXTENSION_CONFIG_FILE', tmp_path / 'extensions.json')
    return result


def execute(factory, catalog, token, **kwargs):
    from app.macro.jobs.macro_extension_runtime import execute_extension_group
    return execute_extension_group(factory, catalog, 'usdcnh', token, **kwargs)


def test_group_success_then_new_instance_same_target_has_zero_http_and_no_db_write(database, tmp_path, monkeypatch):
    file, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        assert count(factory, JobRun) == 1
        return response(200, content=json.dumps({'code': 0, 'data': fx()}).encode())
    result = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(handler), now=NOW)
    assert result['status'] == 'completed' and result['supplierHttp'] == 1
    assert result['insertedRows'] == 1 and result['physicalBudgetEnforced'] is True
    before = file.read_bytes()
    repeated = execute(factory, catalog, '', transport=httpx.MockTransport(handler), now=NOW + timedelta(seconds=1))
    assert repeated['status'] == 'completed' and repeated['supplierHttp'] == 0
    assert len(calls) == 1 and file.read_bytes() == before


def test_group_rate_limiter_is_before_reservation_and_unused_on_reentry(database, tmp_path, monkeypatch):
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    _, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    ticks, waits = [0.0], []
    def sleep(seconds):
        waits.append(seconds)
        ticks[0] += seconds
    limiter = RequestRateLimiter(10, clock=lambda: ticks[0], sleep=sleep)
    limiter.acquire()  # 同一入口前一组已经发过请求。
    transport = httpx.MockTransport(lambda _: response(200, content=json.dumps({'code': 0, 'data': fx()}).encode()))
    first = execute(factory, catalog, 'private-token', transport=transport, now=NOW, rate_limiter=limiter)
    assert first['status'] == 'completed' and waits == [6.0]
    second = execute(factory, catalog, '', transport=transport, now=NOW, rate_limiter=limiter)
    assert second['supplierHttp'] == 0 and waits == [6.0]


def test_response_time_uses_advancing_event_clock_not_job_start(database, tmp_path, monkeypatch):
    from app.macro.extensions.view import load_extension_inputs, build_extension_view
    _, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    tick = [NOW]
    def handler(_):
        tick[0] = NOW + timedelta(minutes=10)
        return response(200, content=json.dumps({'code': 0, 'data': fx()}).encode())
    result = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(handler), clock=lambda: tick[0])
    assert result['status'] == 'completed'
    with factory() as session:
        raw = session.scalar(select(SourcePayload))
        assert raw.payload['retrievedAt'] == (NOW + timedelta(minutes=10)).isoformat()
    earlier = build_extension_view(load_extension_inputs(factory, NOW + timedelta(minutes=1)))
    assert earlier['fx']['value'] is None


def test_permission_failure_is_safe_and_next_day_cannot_retry(database, tmp_path, monkeypatch):
    _, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        return response(200, content=b'{"code":2002,"msg":"private-token private-body","data":null}')
    first = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(handler), now=NOW)
    assert first['errorCode'] == 'EXTENSION_PERMISSION_ERROR' and first['supplierHttp'] == 1
    second = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(handler), now=NOW + timedelta(days=1))
    assert second['status'] == 'blocked' and second['supplierHttp'] == 0 and len(calls) == 1
    assert 'private-token' not in json.dumps([first, second])
    with factory() as session:
        job = session.scalar(select(JobRun))
        assert job.request_count == 1 and job.error_message == 'EXTENSION_PERMISSION_ERROR'
        assert 'private-token' not in json.dumps(job.details)
    assert count(factory, SourcePayload) == 0


def test_replay_received_without_token_and_missing_token_new_writes_nothing(database, tmp_path, monkeypatch):
    file, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    before = file.read_bytes()
    assert execute(factory, catalog, '', now=NOW)['errorCode'] == 'EXTENSION_AUTH_ERROR'
    assert file.read_bytes() == before
    p = plan(tmp_path)
    # plan辅助fixture覆盖了同路径配置，再恢复有意开启的测试配置。
    catalog = enabled(tmp_path, monkeypatch)
    key = store(factory).reserve(p, NOW)
    store(factory).receive(key, fx(), NOW + timedelta(seconds=1))
    result = execute(factory, catalog, '', now=NOW + timedelta(days=1))
    assert result['supplierHttp'] == 0 and result['status'] == 'completed'
    assert count(factory, MacroObservation) == 1 and count(factory, JobRun) == 1


def test_disabled_flag_is_reloaded_before_reservation_or_transport(database, tmp_path, monkeypatch):
    _, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    config(tmp_path)  # 正式发送前文件已被用户关闭，传入旧catalog不授权新请求。
    calls = []
    result = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(lambda req: calls.append(req)), now=NOW)
    assert result['status'] in ('blocked', 'disabled') and result['supplierHttp'] == 0
    assert count(factory, JobRun) == 0 and calls == []


def test_cli_dry_run_preserves_real_sqlite_and_has_only_dry_run_option(database, tmp_path, monkeypatch):
    file, _ = database
    enabled(tmp_path, monkeypatch)
    monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
    monkeypatch.setenv('DANG_DATABASE_URL', f'sqlite:///{file.as_posix()}')
    monkeypatch.setenv('DANG_DATABASE_READ_URL', f'sqlite:///{file.as_posix()}')
    before = file.read_bytes()
    result = CliRunner().invoke(app, ['sync-macro-extensions', '--dry-run'])
    assert result.exit_code == 0, result.stdout
    value = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{'))
    assert value['supplierHttp'] == 0 and value['readOnly'] is True
    assert value['executorImplemented'] is True and value['physicalBudgetEnforced'] is False
    assert file.read_bytes() == before
    formal = CliRunner().invoke(app, ['sync-macro-extensions'])
    assert formal.exit_code == 2 and file.read_bytes() == before
    for flag in ('--force', '--source', '--as-of', '--publish'):
        assert CliRunner().invoke(app, ['sync-macro-extensions', flag]).exit_code == 2


def test_cli_errors_never_echo_secret_or_bad_config(database, tmp_path, monkeypatch):
    file, _ = database
    monkeypatch.setenv('DANG_DATA_BACKEND', 'database')
    monkeypatch.setenv('DANG_DATABASE_URL', f'sqlite:///{file.as_posix()}')
    broken = tmp_path / 'broken.json'
    broken.write_text('{"password":"private-token"}', encoding='utf-8')
    monkeypatch.setattr(extension_config, 'DEFAULT_EXTENSION_CONFIG_FILE', broken)
    result = CliRunner().invoke(app, ['sync-macro-extensions', '--dry-run'])
    assert result.exit_code == 2
    assert 'private-token' not in result.stdout + result.stderr


@pytest.mark.parametrize('failure', ['transport', 'failure-persistence'])
def test_unknown_failure_never_reports_zero_budget_after_committed_reservation(database, tmp_path, monkeypatch, failure):
    from sqlalchemy.exc import SQLAlchemyError
    from app.macro.extensions.store import ExtensionStore
    _, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    def handler(_):
        if failure == 'transport': raise RuntimeError('private-token')
        return response(403, content=b'private-token')
    if failure == 'failure-persistence':
        def fail(*args, **kwargs): raise SQLAlchemyError('private-token')
        monkeypatch.setattr(ExtensionStore, 'fail', fail)
    result = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(handler), now=NOW)
    assert result['status'] == 'blocked' and result['supplierHttp'] == 1
    assert result['readOnly'] is False and result['planPersisted'] is True
    assert 'private-token' not in json.dumps(result)
    assert count(factory, JobRun) == 1


@pytest.mark.parametrize('confirmation_unavailable', [False, True])
def test_commit_ack_loss_preserves_known_run_id_and_reservation_upper_bound(database, tmp_path, monkeypatch, confirmation_unavailable):
    from sqlalchemy.exc import SQLAlchemyError
    from app.macro.extensions.store import ExtensionStore
    _, factory = database
    catalog = enabled(tmp_path, monkeypatch)
    original_reserve, original_inspect = ExtensionStore.reserve, ExtensionStore.inspect
    committed = []
    def lose_ack(self, p, at):
        committed.append(original_reserve(self, p, at))
        raise SQLAlchemyError('private-token-commit-ack')
    def inspect_after(self, *args):
        if committed and confirmation_unavailable: raise SQLAlchemyError('private-token-offline')
        return original_inspect(self, *args)
    monkeypatch.setattr(ExtensionStore, 'reserve', lose_ack)
    monkeypatch.setattr(ExtensionStore, 'inspect', inspect_after)
    calls = []
    result = execute(factory, catalog, 'private-token', transport=httpx.MockTransport(lambda req: calls.append(req)), now=NOW)
    assert result['status'] == 'blocked' and result['requestCount'] == 1
    assert result['supplierHttp'] == 0 and result['runId'] == committed[0]
    assert result['planPersisted'] is (None if confirmation_unavailable else True)
    assert result['readOnly'] is False and calls == [] and 'private-token' not in json.dumps(result)
