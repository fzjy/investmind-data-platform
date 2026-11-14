"""公开官方文档显式gzip能力；原金融入口仍默认identity-only。"""
import asyncio
import gzip
import random

import httpx
import pytest

from app.macro.extensions.client import ExtensionClient
from app.macro.extensions.config import ExtensionError
from backend.tests.macro.test_extension_client import response


def read(body,*,encoding='gzip',limit=1000,enabled=True):
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.headers['Accept-Encoding']=='identity'
        return response(200,content=body,headers={'Content-Encoding':encoding})
    client=ExtensionClient('public-official-document',transport=httpx.MockTransport(handle))
    result=asyncio.run(client._read({'limits':{'requestTimeoutSeconds':1,'maxResponseBytes':limit}},
        'GET','https://www.nffund.com/',gzip_response_allowed=enabled))
    assert len(calls)==1
    return bytes(result)


def test_explicit_gzip_returns_the_original_decoded_document():
    original='<html>基金512400</html>'.encode()
    assert read(gzip.compress(original))==original


def test_identity_remains_accepted_when_gzip_capability_is_enabled():
    assert read(b'<html/>',encoding='identity')==b'<html/>'


def test_financial_default_still_rejects_compressed_responses():
    client=ExtensionClient('secret',transport=httpx.MockTransport(lambda r:response(200,
        content=gzip.compress(b'{}'),headers={'Content-Encoding':'gzip'})))
    with pytest.raises(ExtensionError,match='^EXTENSION_ENCODING_UNSUPPORTED$'):
        asyncio.run(client._read({'limits':{'requestTimeoutSeconds':1,'maxResponseBytes':100}},'GET','https://api.tushare.pro'))


@pytest.mark.parametrize('body',[gzip.compress(b'a'*2000),b'x'*1001])
def test_wire_and_decoded_lengths_are_independently_limited(body):
    with pytest.raises(ExtensionError,match='^EXTENSION_RESPONSE_TOO_LARGE$'):read(body)


@pytest.mark.parametrize('body',[b'not gzip',gzip.compress(b'abc')[:-3],gzip.compress(b'abc')+b'junk',gzip.compress(b'a')+gzip.compress(b'b')])
def test_invalid_truncated_trailing_and_concatenated_streams_are_rejected(body):
    with pytest.raises(ExtensionError,match='^EXTENSION_RESPONSE_INVALID$'):read(body)


@pytest.mark.parametrize('encoding',['br','gzip, br','deflate'])
def test_other_encoding_formats_are_not_silently_accepted(encoding):
    with pytest.raises(ExtensionError,match='^EXTENSION_ENCODING_UNSUPPORTED$'):read(b'abc',encoding=encoding)


def test_exact_decoded_limit_and_multiple_wire_chunks():
    assert read(gzip.compress(b'a'*1000))==b'a'*1000
    original=random.Random(72).randbytes(9000)
    assert read(gzip.compress(original),limit=10000)==original


def test_bad_crc_is_not_accepted_as_a_document():
    body=bytearray(gzip.compress(b'abc'));body[-8]^=1
    with pytest.raises(ExtensionError,match='^EXTENSION_RESPONSE_INVALID$'):read(bytes(body))


@pytest.mark.parametrize('enabled',[1,'true',None])
def test_gzip_opt_in_requires_an_actual_boolean(enabled):
    with pytest.raises(ExtensionError,match='^EXTENSION_RESPONSE_INVALID$'):read(b'abc',enabled=enabled)
