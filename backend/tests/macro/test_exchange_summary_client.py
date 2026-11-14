import asyncio
from importlib import import_module
import json

import httpx
import pytest

from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_exchange_summary_plan import build
from backend.tests.macro.test_exchange_summary_normalize import business

TOKEN='TEST_PRIVATE_TOKEN_NOT_FOR_LOGS'


def response(status=200,*,value=None,content=None,headers=None):
    body=json.dumps(value).encode() if value is not None else (content or b'')
    return httpx.Response(status,headers=headers,stream=httpx.ByteStream(body))


def call(handler,p=None):
    p=p or build()
    client=import_module('app.macro.exchange_summary_client').ExchangeSummaryClient(TOKEN,transport=httpx.MockTransport(handler))
    return asyncio.run(client.fetch(p,p['documents'][0]))


def test_minimum_rpc_exact_scope_and_safe_business_only():
    calls=[]
    def handler(request):
        calls.append(request);payload=json.loads(request.content)
        assert payload=={'api_name':'margin','token':TOKEN,'params':{'trade_date':'20261022','exchange_id':'SSE'},'fields':'trade_date,exchange_id,rzye'}
        assert str(request.url)=='https://api.tushare.pro' and request.method=='POST'
        return response(value={'code':0,'msg':'ignored provider metadata','request_id':'not retained','data':business()})
    result=call(handler)
    assert result==business() and len(calls)==1 and TOKEN not in json.dumps(result)


@pytest.mark.parametrize('kind',['permission','echo-msg','echo-business','duplicate-json','has-more','redirect','large','timeout','server','invalid-json'])
def test_error_bodies_and_echoed_token_never_escape_or_retry(kind):
    calls=[]
    def handler(request):
        calls.append(request)
        if kind=='timeout':raise httpx.ReadTimeout(TOKEN)
        if kind=='permission':return response(value={'code':2002,'msg':TOKEN})
        if kind=='redirect':return response(302,headers={'location':'https://untrusted.invalid/?token='+TOKEN})
        if kind=='server':return response(503,content=TOKEN.encode())
        if kind=='large':return response(content=b'x'*(1048576+1))
        if kind=='duplicate-json':return response(content=b'{"code":0,"code":0}')
        if kind=='invalid-json':return response(content=TOKEN.encode())
        data=business();msg=TOKEN if kind=='echo-msg' else ''
        if kind=='echo-business':data['items'][0][2]=TOKEN
        if kind=='has-more':data['has_more']=True
        return response(value={'code':0,'msg':msg,'data':data})
    with pytest.raises(ExtensionError) as caught:call(handler)
    assert TOKEN not in str(caught.value) and len(calls)==1


def test_failed_business_identity_is_raw_replayable_not_new_http_diagnosis():
    bad=business();bad['items'][0][1]='WRONG'
    assert call(lambda _:response(value={'code':0,'data':bad}))==bad


def test_provider_large_integer_is_saved_as_lossless_business_amount_text():
    data=business(value=9007199254740993)
    result=call(lambda _:response(value={'code':0,'data':data}))
    assert result['items'][0][2]=='9007199254740993'
