"""仅MockTransport模拟供应商，实际客户端负责流读取及单次发送。"""
from dataclasses import replace
from importlib import import_module
import json

import httpx
import pytest
from app.macro.target_evidence_contract import TargetEvidenceRequest,API_FIELDS
from backend.tests.macro.test_target_evidence_policy import policy


def module():
    try:return import_module('app.connectors.tushare.target_execution_client')
    except ImportError:pytest.fail('bounded streaming target client not implemented')


class Stream(httpx.SyncByteStream):
    def __init__(self,chunks):self.chunks=chunks;self.closed=False;self.reads=0
    def __iter__(self):
        for chunk in self.chunks:self.reads+=1;yield chunk
    def close(self):self.closed=True


def request():return TargetEvidenceRequest('000300.SH','index','index_basic',{'ts_code':'000300.SH','market':'CSI'},API_FIELDS['index_basic'])
def options(**kw):return replace(policy(),collection_enabled=True,**kw)
def body():return {'code':0,'data':{'fields':list(API_FIELDS['index_basic']),'items':[['000300.SH','300','300','CSI','中证',None]]}}


def test_reservation_precedes_single_stream_and_rejected_reservation_sends_nothing():
    mod=module();events=[];stream=Stream([json.dumps(body()).encode()])
    def send(req):events.append('send');assert events==['reserved','send'];assert req.url.scheme=='https';return httpx.Response(200,stream=stream)
    with mod.TargetExecutionClient('controlled-token',options(),transport=httpx.MockTransport(send)) as client:
        result=client.call(request(),lambda:events.append('reserved'))
    assert result.items[0][0]=='000300.SH' and stream.closed and events==['reserved','send']
    calls=[]
    def blocked():raise ValueError('TARGET_ROOT_BUDGET_EXHAUSTED')
    with mod.TargetExecutionClient('controlled-token',options(),transport=httpx.MockTransport(lambda req:calls.append(req))) as client:
        with pytest.raises(ValueError,match='TARGET_ROOT_BUDGET_EXHAUSTED'):client.call(request(),blocked)
    assert calls==[]


@pytest.mark.parametrize(('status','code'),[(301,'TARGET_REDIRECT_REFUSED'),(307,'TARGET_REDIRECT_REFUSED'),
    (401,'TARGET_AUTH_ERROR'),(403,'TARGET_AUTH_ERROR'),(429,'TARGET_RATE_LIMITED'),(500,'TARGET_SERVER_ERROR'),(400,'TARGET_HTTP_ERROR')])
def test_failure_status_never_retries_or_follows_redirect(status,code):
    mod=module();calls=[];stream=Stream([b'private-token'])
    def send(req):calls.append(req);return httpx.Response(status,headers={'Location':'https://other.invalid'},stream=stream)
    with mod.TargetExecutionClient('private-token',options(),transport=httpx.MockTransport(send)) as client:
        with pytest.raises(mod.TargetExecutionError,match=code) as caught:client.call(request(),lambda:None)
    assert len(calls)==1 and stream.closed and 'private-token' not in str(caught.value)


@pytest.mark.parametrize('headers',[{}, {'Content-Length':'1000'}, {'Content-Encoding':'gzip'}])
def test_stream_limit_or_encoding_refusal_always_closes_without_full_read(headers):
    mod=module();stream=Stream([b'x'*32]*20)
    with mod.TargetExecutionClient('token',options(max_response_bytes=64),transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers=headers,stream=stream))) as client:
        with pytest.raises(mod.TargetExecutionError,match='TARGET_(RESPONSE_TOO_LARGE|ENCODING_UNSUPPORTED)'):
            client.call(request(),lambda:None)
    assert stream.closed and stream.reads<20


@pytest.mark.parametrize('raw',[
    b'not json',b'\xff',b'{"code":0,"code":0,"data":{}}',b'{"code":NaN,"data":{}}',
    b'{"code":false,"data":{}}',b'{"data":{}}',b'{"code":0,"data":{"fields":[],"items":[]}}',
])
def test_invalid_response_never_leaks_body_or_token(raw):
    mod=module();stream=Stream([raw])
    with mod.TargetExecutionClient('sensitive-token',options(),transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=stream))) as client:
        with pytest.raises(mod.TargetExecutionError,match='TARGET_RESPONSE_INVALID') as caught:client.call(request(),lambda:None)
    assert stream.closed and 'sensitive-token' not in repr(caught.value)


@pytest.mark.parametrize(('message','code'),[('接口权限不足 sensitive-token','TARGET_PERMISSION_ERROR'),
    ('token sensitive-token 无效','TARGET_AUTH_ERROR'),('每分钟次数受限 sensitive-token','TARGET_RATE_LIMITED'),
    ('private provider details sensitive-token','TARGET_RESPONSE_INVALID')])
def test_provider_error_messages_are_classified_but_never_returned(message,code):
    mod=module();stream=Stream([json.dumps({'code':-1,'msg':message,'data':None}).encode()])
    with mod.TargetExecutionClient('sensitive-token',options(),transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=stream))) as client:
        with pytest.raises(mod.TargetExecutionError,match=code) as caught:client.call(request(),lambda:None)
    assert message not in str(caught.value) and 'sensitive-token' not in repr(caught.value)


def test_timeout_and_row_limit_have_no_hidden_retry():
    mod=module();calls=[]
    def timeout(req):calls.append(req);raise httpx.ReadTimeout('sensitive-token and body')
    with mod.TargetExecutionClient('sensitive-token',options(),transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(mod.TargetExecutionError,match='TARGET_NETWORK_ERROR') as caught:client.call(request(),lambda:None)
    assert len(calls)==1 and 'sensitive-token' not in str(caught.value)
    value=body();value['data']['items']*=2
    with mod.TargetExecutionClient('token',options(max_response_rows=1),transport=httpx.MockTransport(
        lambda req:httpx.Response(200,stream=Stream([json.dumps(value).encode()])))) as client:
        with pytest.raises(mod.TargetExecutionError,match='TARGET_RESPONSE_INVALID'):client.call(request(),lambda:None)
