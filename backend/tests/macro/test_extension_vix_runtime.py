"""FRED真实httpx流/日志和持久预留；远端为完整受控HTTP边界。"""
from datetime import timedelta
import json
import logging

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli.main import app
from app.config import Settings
from app.db.models.ops import JobRun
from app.macro.jobs.macro_extension_runtime import execute_extension_group
from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.store import ExtensionStore
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_plan import plan
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_vix import VIX_NOW, vix_config, vix_plan, vix_rows


def fred_payload():
    return {'realtime_start': '2026-10-24', 'realtime_end': '2026-10-24',
        'observation_start': '2026-04-27', 'observation_end': '2026-10-23',
        'units': 'lin', 'output_type': 1, 'file_type': 'json', 'order_by': 'observation_date',
        'sort_order': 'asc', 'count': 3, 'offset': 0, 'limit': 366, 'observations': [
            {'date': '2026-10-21', 'value': '10.25', 'realtime_start': '2026-10-24', 'realtime_end': '2026-10-24'},
            {'date': '2026-10-22', 'value': '.', 'realtime_start': '2026-10-24', 'realtime_end': '2026-10-24'},
            {'date': '2026-10-23', 'value': '12.50', 'realtime_start': '2026-10-24', 'realtime_end': '2026-10-24'}]}


def client(transport):
    from app.macro.extensions.fred_client import FredExtensionClient
    return FredExtensionClient('fred-private-key', transport=transport)


def test_fred_committed_budget_precedes_get_and_query_secret_never_enters_logs(database, tmp_path, caplog):
    _, factory = database
    p = vix_plan(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET' and str(request.url).split('?')[0] == 'https://api.stlouisfed.org/fred/series/observations'
        assert request.url.params['api_key'] == 'fred-private-key'
        assert request.url.params['series_id'] == 'VIXCLS'
        assert ExtensionStore(factory).inspect(p, VIX_NOW)['status'] == 'uncertain'
        return response(200, content=json.dumps(fred_payload()).encode())
    with caplog.at_level(logging.INFO, logger='httpx'):
        result = client(httpx.MockTransport(handler)).call(p, lambda: ExtensionStore(factory).reserve(p, VIX_NOW))
    assert result == vix_rows() and len(calls) == 1
    assert 'fred-private-key' not in caplog.text and 'fred-private-key' not in json.dumps(result)
    assert 'HTTP Request' in caplog.text


@pytest.mark.parametrize('key,value', [('count', 4), ('count', True), ('offset', 1), ('limit', 100000),
    ('units', 'pc1'), ('output_type', True), ('sort_order', 'desc'), ('realtime_end', '2026-10-25'),
    ('observation_end', '2026-10-24')])
def test_fred_pagination_or_contract_mismatch_is_one_safe_failure(tmp_path, key, value):
    p = vix_plan(tmp_path)
    raw = fred_payload(); raw[key] = value
    calls = []
    def handler(request):
        calls.append(request)
        return response(200, content=json.dumps(raw).encode())
    with pytest.raises(ExtensionError, match='^EXTENSION_RESPONSE_INVALID$'):
        client(httpx.MockTransport(handler)).call(p, lambda: None)
    assert len(calls) == 1


@pytest.mark.parametrize('status,code', [(302, 'EXTENSION_REDIRECT_REFUSED'), (401, 'EXTENSION_AUTH_ERROR'),
    (429, 'EXTENSION_RATE_LIMITED'), (503, 'EXTENSION_SERVER_ERROR')])
def test_fred_http_errors_do_not_redirect_retry_or_echo_query_key(tmp_path, status, code, caplog):
    p = vix_plan(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return response(status, content=b'fred-private-key private-body', headers={'location': 'https://other.invalid/?api_key=fred-private-key'})
    with caplog.at_level(logging.INFO, logger='httpx'):
        with pytest.raises(ExtensionError, match=f'^{code}$'):
            client(httpx.MockTransport(handler)).call(p, lambda: None)
    assert len(calls) == 1 and 'fred-private-key' not in caplog.text


def test_wrong_provider_client_rejects_before_reservation_or_transmission(tmp_path):
    p, old = vix_plan(tmp_path), plan(tmp_path)
    calls, reservations = [], []
    transport = httpx.MockTransport(lambda request: calls.append(request))
    with pytest.raises(ExtensionError, match='^EXTENSION_EXECUTION_INVALID$'):
        ExtensionClient('fred-private-key', transport=transport).call(p, lambda: reservations.append(1))
    with pytest.raises(ExtensionError, match='^EXTENSION_EXECUTION_INVALID$'):
        client(transport).call(old, lambda: reservations.append(1))
    assert calls == [] and reservations == []


@pytest.mark.parametrize('logger_name', ['httpcore.http11', 'httpcore.http2', 'httpcore.connection', 'httpcore.proxy', 'httpcore.socks'])
def test_fred_low_level_response_headers_redact_query_credentials(tmp_path, caplog, logger_name):
    p = vix_plan(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        logging.getLogger(logger_name).debug('receive_response_headers.complete return_value=%r',
            (b'HTTP/1.1', 302, b'Found', [(b'Location', str(request.url).encode())]))
        return response(302, headers={'location': str(request.url)})
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        with pytest.raises(ExtensionError, match='^EXTENSION_REDIRECT_REFUSED$'):
            client(httpx.MockTransport(handler)).call(p, lambda: None)
    assert len(calls) == 1
    assert 'receive_response_headers.complete' in caplog.text
    assert 'fred-private-key' not in caplog.text


def test_vix_executor_stores_once_and_reentry_does_not_call_http(database, tmp_path, monkeypatch):
    file, factory = database
    config = vix_config(tmp_path)
    from app.macro.extensions import config as module
    monkeypatch.setattr(module, 'DEFAULT_EXTENSION_CONFIG_FILE', tmp_path / 'extensions.json')
    calls = []
    def handler(request):
        calls.append(request)
        return response(200, content=json.dumps(fred_payload()).encode())
    first = execute_extension_group(factory, config, 'vix', 'fred-private-key', transport=httpx.MockTransport(handler), now=VIX_NOW)
    assert first['status'] == 'completed' and first['supplierHttp'] == 1 and first['insertedRows'] == 2
    before = file.read_bytes()
    again = execute_extension_group(factory, config, 'vix', '', transport=httpx.MockTransport(handler), now=VIX_NOW + timedelta(seconds=1))
    assert again['supplierHttp'] == 0 and file.read_bytes() == before and len(calls) == 1


@pytest.mark.parametrize('groups', [[], ['not-a-source'], ['vix', 'vix']])
def test_unknown_or_duplicate_group_fails_before_database(monkeypatch, groups):
    from app.macro.jobs import macro_extension_runtime as module
    monkeypatch.setattr(module, 'create_engine_and_session', lambda *_: pytest.fail('invalid group opened database'))
    with pytest.raises(ExtensionError):
        module.run_macro_extensions(Settings(), group_ids=groups)


def test_group_cli_option_exists_without_force_or_clock_override():
    result = CliRunner().invoke(app, ['sync-macro-extensions', '--help'])
    assert result.exit_code == 0 and '--group-id' in result.stdout and '--dry-run' in result.stdout
    assert CliRunner().invoke(app, ['sync-macro-extensions', '--force']).exit_code == 2


def test_recovery_resolves_fred_secret_and_never_sends_tushare_secret(database, tmp_path, monkeypatch):
    from backend.tests.macro.test_extension_recovery_runtime import controlled_runtime
    _, factory = database
    p = vix_plan(tmp_path)
    from app.macro.extensions import config as configuration
    monkeypatch.setattr(configuration, 'DEFAULT_EXTENSION_CONFIG_FILE', tmp_path / 'extensions.json')
    parent = ExtensionStore(factory).reserve(p, VIX_NOW)
    ExtensionStore(factory).fail(parent, 'EXTENSION_RESPONSE_INVALID', VIX_NOW + timedelta(seconds=1))
    module, _ = controlled_runtime(factory, monkeypatch)
    settings = Settings(data_backend='database', database_url='postgresql+psycopg://controlled/isolated',
        tushare_token='tushare-private', fred_api_key='fred-private-key')
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.params['api_key'] == 'fred-private-key'
        assert 'tushare-private' not in str(request.url)
        return response(200, content=json.dumps(fred_payload()).encode())
    result = module.run_macro_extension_recovery(settings, [parent], transport=httpx.MockTransport(handler), now=VIX_NOW + timedelta(seconds=2))
    assert result['status'] == 'completed' and result['supplierHttp'] == 1 and len(calls) == 1
    from app.db.models.raw import SourcePayload
    with factory() as session:
        raw = session.scalar(select(SourcePayload))
        assert 'fred-private-key' not in json.dumps(raw.payload) and 'tushare-private' not in json.dumps(raw.payload)
