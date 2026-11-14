"""真实httpx有界流，外部网络用完整MockTransport代替。"""
import json

import httpx
import pytest

from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_evidence import fx
from backend.tests.macro.test_extension_plan import plan, payload
from backend.tests.macro.test_extension_store import database, NOW, store, count
from app.db.models.ops import JobRun


def client(transport):
    from app.macro.extensions.client import ExtensionClient
    return ExtensionClient('do-not-echo-token', transport=transport)


def response(status, *, content=b'', headers=None):
    # HTTPX的content=会预先消费mock响应；模拟真实stream而非测试专用生产分支。
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))


def test_committed_reservation_precedes_real_client_request_and_decimal_is_not_float(database, tmp_path):
    _, factory = database
    p = plan(tmp_path)
    requests = []
    def handler(request):
        requests.append(request)
        assert count(factory, JobRun) == 1
        assert store(factory).inspect(p, NOW)['status'] == 'uncertain'
        assert str(request.url) == 'https://api.tushare.pro'
        assert json.loads(request.content) == {'api_name': 'fx_daily', 'token': 'do-not-echo-token',
            'params': {'ts_code': 'USDCNH.FXCM', 'exchange': 'FXCM', 'start_date': '20260724', 'end_date': '20261021'},
            'fields': 'ts_code,trade_date,exchange,bid_close,ask_close'}
        return response(200, content=b'{"code":0,"data":{"fields":["ts_code","trade_date","exchange","bid_close","ask_close"],"items":[["USDCNH.FXCM","20261021","FXCM",7.123456789012345678,7.13]]}}')
    with client(httpx.MockTransport(handler)) as transport:
        data = transport.call(p, lambda: store(factory).reserve(p, NOW))
    assert data['items'][0][3] == '7.123456789012345678'
    assert len(requests) == 1


@pytest.mark.parametrize('status,code', [(302, 'EXTENSION_REDIRECT_REFUSED'), (403, 'EXTENSION_AUTH_ERROR'),
    (429, 'EXTENSION_RATE_LIMITED'), (503, 'EXTENSION_SERVER_ERROR'), (404, 'EXTENSION_HTTP_ERROR')])
def test_http_failure_never_redirects_retries_or_echoes_body(tmp_path, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return response(status, headers={'location': 'https://private.invalid'}, content=b'do-not-echo-token private-body')
    with client(httpx.MockTransport(handler)) as transport:
        with pytest.raises(ExtensionError, match=f'^{code}$'):
            transport.call(plan(tmp_path), lambda: None)
    assert len(calls) == 1


@pytest.mark.parametrize('body,headers,code', [
    (b'{"code":2002,"msg":"do-not-echo-token","data":null}', {}, 'EXTENSION_PERMISSION_ERROR'),
    (b'{"code":true,"data":null}', {}, 'EXTENSION_RESPONSE_INVALID'),
    (b'{"code":0,"code":0,"data":null}', {}, 'EXTENSION_RESPONSE_INVALID'),
    (b'{"code":0,"data":NaN}', {}, 'EXTENSION_RESPONSE_INVALID'),
    (b'private', {'content-encoding': 'gzip'}, 'EXTENSION_ENCODING_UNSUPPORTED'),
    (b'private', {'content-length': '8388609'}, 'EXTENSION_RESPONSE_TOO_LARGE'),
    (b'private', {'content-length': 'unknown'}, 'EXTENSION_RESPONSE_INVALID'),
])
def test_bad_outer_response_or_header_has_safe_code(tmp_path, body, headers, code):
    with client(httpx.MockTransport(lambda _: response(200, headers=headers, content=body))) as transport:
        with pytest.raises(ExtensionError, match=f'^{code}$'):
            transport.call(plan(tmp_path), lambda: None)


def test_stream_byte_limit_without_content_length_and_zero_network_before_failed_reservation(tmp_path):
    raw = payload()
    raw['groups']['usdcnh']['limits']['maxResponseBytes'] = 100
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'x' * 101))
    with client(httpx.MockTransport(handler)) as transport:
        with pytest.raises(ExtensionError, match='^EXTENSION_RESPONSE_TOO_LARGE$'):
            transport.call(plan(tmp_path, value=raw), lambda: None)
        def denied(): raise ExtensionError('EXTENSION_ALREADY_RESERVED')
        with pytest.raises(ExtensionError, match='^EXTENSION_ALREADY_RESERVED$'):
            transport.call(plan(tmp_path), denied)
    assert len(calls) == 1


def test_disconnect_is_one_request_and_safe(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadError('do-not-echo-token')
    with client(httpx.MockTransport(handler)) as transport:
        with pytest.raises(ExtensionError, match='^EXTENSION_NETWORK_ERROR$'):
            transport.call(plan(tmp_path), lambda: None)
    assert len(calls) == 1


def test_total_async_deadline_cancels_slow_response_without_retry(tmp_path):
    import asyncio
    import time
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(2)
            yield b'private-body'
    raw = payload()
    raw['groups']['usdcnh']['limits']['requestTimeoutSeconds'] = 1
    started = time.monotonic()
    with client(httpx.MockTransport(lambda _: httpx.Response(200, stream=Slow()))) as transport:
        with pytest.raises(ExtensionError, match='^EXTENSION_TIMEOUT$'):
            transport.call(plan(tmp_path, value=raw), lambda: None)
    assert time.monotonic() - started < 1.9


@pytest.mark.parametrize('group,fields,items', [
    ('usdcnh', ['ts_code', 'trade_date', 'exchange', 'bid_close', 'ask_close'], []),
    ('nominalGdp', ['quarter', 'gdp', 'gdp_yoy'], [['2026Q2', '260', '5.5']]),
])
def test_transport_metadata_does_not_reject_valid_fields_items(tmp_path, group, fields, items):
    # 真实诊断见证额外传输元数据；内容值为受控数据，不复制真实响应。
    envelope = {'code': 0, 'msg': 'private-token', 'data': {
        'fields': fields, 'items': items, 'has_more': False, 'opaque': {'private': 'value'}}}
    content = json.dumps(envelope).encode()
    with client(httpx.MockTransport(lambda _: response(200, content=content))) as transport:
        result = transport.call(plan(tmp_path, group), lambda: None)
    assert result == {'fields': fields, 'items': items}
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('more', [True, 0, 'false', None])
def test_incomplete_or_ambiguous_pagination_is_rejected_without_followup(tmp_path, more):
    envelope = {'code': 0, 'data': {**fx(), 'has_more': more}}
    calls = []
    def handler(request):
        calls.append(request)
        return response(200, content=json.dumps(envelope).encode())
    with client(httpx.MockTransport(handler)) as transport:
        with pytest.raises(ExtensionError, match='^EXTENSION_RESPONSE_INVALID$'):
            transport.call(plan(tmp_path), lambda: None)
    assert len(calls) == 1
