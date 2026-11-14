import asyncio
import httpx
import pytest

from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_client import response

PLAN={'limits':{'requestTimeoutSeconds':20,'maxResponseBytes':8388608}}


def test_official_form_uses_standard_urlencoded_body_without_changing_json_default():
    calls=[]
    def send(request):
        calls.append(request)
        assert request.headers['content-type']=='application/x-www-form-urlencoded'
        assert request.content==b'fundCode=512400&queryDate=2026-10-28'
        return response(200,content=b'{"code":"ETS-5BP00000"}')
    client=ExtensionClient('unused',transport=httpx.MockTransport(send))
    assert asyncio.run(client._read(PLAN,'POST','https://www.nffund.com/nfwebApi/trade/subAndRedempList',
        form={'fundCode':'512400','queryDate':'2026-10-28'}))==b'{"code":"ETS-5BP00000"}'
    assert len(calls)==1


@pytest.mark.parametrize('kwargs',[
    {'form':{'fundCode':'512400'},'payload':{'fundCode':'512400'}},
    {'form':['512400']},{'form':{'fundCode':512400}},{'form':{}},
])
def test_invalid_or_ambiguous_form_body_sends_nothing(kwargs):
    calls=[];client=ExtensionClient('unused',transport=httpx.MockTransport(lambda request:calls.append(request)))
    with pytest.raises(ExtensionError,match='EXTENSION_RESPONSE_INVALID'):
        asyncio.run(client._read(PLAN,'POST','https://www.nffund.com/nfwebApi/fund/overreview',**kwargs))
    assert calls==[]


def test_form_is_not_sent_as_get_payload():
    client=ExtensionClient('unused')
    with pytest.raises(ExtensionError,match='EXTENSION_RESPONSE_INVALID'):
        asyncio.run(client._read(PLAN,'GET','https://www.nffund.com/',form={'fundCode':'512400'}))
