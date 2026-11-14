"""固定HTTPS、一次发送、总期限与raw流上限；错误不带供应商正文。"""
import asyncio
import json
import zlib

import httpx

from app.macro.extensions.config import ExtensionError
from app.macro.extensions.normalize import normalize_extension_response
from app.macro.extensions.plan import validate_extension_plan


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExtensionError('EXTENSION_RESPONSE_INVALID')
        result[key] = value
    return result


def _constant(_):
    raise ExtensionError('EXTENSION_RESPONSE_INVALID')


class ExtensionClient:
    source = 'tushare'

    def __init__(self, token: str, *, transport=None):
        if type(token) is not str or not token:
            raise ExtensionError('EXTENSION_AUTH_ERROR')
        self._token = token
        self._transport = transport

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def call(self, plan: dict, before_send) -> dict:
        validate_extension_plan(plan)
        if plan['source'] != self.source or not callable(before_send):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        before_send()  # 事务必须已提交；失败直接传播，根本不创建HTTP会话。
        return asyncio.run(self._request(plan))

    async def _request(self, plan):
        payload = {'api_name': plan['query']['api'], 'token': self._token,
            'params': plan['query']['params'], 'fields': ','.join(plan['query']['fields'])}
        body = await self._read(plan, 'POST', 'https://api.tushare.pro', payload=payload)
        try:
            value = decode_json(body)
            if type(value) is not dict or type(value.get('code')) is not int:
                raise ExtensionError('EXTENSION_RESPONSE_INVALID')
            if value['code'] != 0:
                raise ExtensionError('EXTENSION_PERMISSION_ERROR' if value['code'] == 2002 else 'EXTENSION_PROVIDER_REJECTED')
            data = value['data']
            if type(data) is not dict or ('has_more' in data and data['has_more'] is not False):
                raise ExtensionError('EXTENSION_RESPONSE_INVALID')
            business_data = {'fields': data['fields'], 'items': data['items']}
            normalize_extension_response(plan, business_data)
            return business_data
        except ExtensionError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None

    async def _read(self, plan, method, url, *, payload=None, params=None, gzip_response_allowed=False,form=None):
        limits = plan['limits']
        if type(gzip_response_allowed) is not bool:raise ExtensionError('EXTENSION_RESPONSE_INVALID')
        if form is not None and (method!='POST' or payload is not None or type(form) is not dict or not form
            or len(form)>16 or any(type(k) is not str or not 1<=len(k)<=100 or type(v) is not str or len(v)>2048 for k,v in form.items())):
            raise ExtensionError('EXTENSION_RESPONSE_INVALID')
        request_body={'json':payload} if form is None else {'data':form}
        try:
            async with asyncio.timeout(limits['requestTimeoutSeconds']):
                async with httpx.AsyncClient(transport=self._transport if self._transport is not None else httpx.AsyncHTTPTransport(retries=0),
                    timeout=limits['requestTimeoutSeconds'], follow_redirects=False, trust_env=False) as client:
                    async with client.stream(method, url, **request_body, params=params,
                        headers={'Accept-Encoding': 'identity'}) as response:
                        status = response.status_code
                        if 300 <= status < 400: raise ExtensionError('EXTENSION_REDIRECT_REFUSED')
                        if status in (401, 403): raise ExtensionError('EXTENSION_AUTH_ERROR')
                        if status == 429: raise ExtensionError('EXTENSION_RATE_LIMITED')
                        if status >= 500: raise ExtensionError('EXTENSION_SERVER_ERROR')
                        if status >= 400: raise ExtensionError('EXTENSION_HTTP_ERROR')
                        encoding=response.headers.get('content-encoding', 'identity').strip().lower()
                        decoder=None
                        if encoding not in ('', 'identity'):
                            if not gzip_response_allowed or encoding!='gzip':raise ExtensionError('EXTENSION_ENCODING_UNSUPPORTED')
                            decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
                        length = response.headers.get('content-length')
                        if length is not None:
                            if not length.isascii() or not length.isdecimal() or len(length) > 20:
                                raise ExtensionError('EXTENSION_RESPONSE_INVALID')
                            if int(length) > limits['maxResponseBytes']:
                                raise ExtensionError('EXTENSION_RESPONSE_TOO_LARGE')
                        body = bytearray()
                        wire_size=0
                        async for chunk in response.aiter_raw(chunk_size=min(8192, limits['maxResponseBytes'] + 1)):
                            wire_size+=len(chunk)
                            if wire_size > limits['maxResponseBytes']:
                                raise ExtensionError('EXTENSION_RESPONSE_TOO_LARGE')
                            if decoder is not None:
                                chunk=decoder.decompress(chunk,limits['maxResponseBytes']-len(body)+1)
                            if len(body)+len(chunk)>limits['maxResponseBytes']:
                                raise ExtensionError('EXTENSION_RESPONSE_TOO_LARGE')
                            body.extend(chunk)
                        if decoder is not None:
                            # 完整gzip在decompress中到达eof；flush的length不是输出上限，不能用于有界读取。
                            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                                raise ExtensionError('EXTENSION_RESPONSE_INVALID')
            return body
        except (TimeoutError, httpx.TimeoutException):
            raise ExtensionError('EXTENSION_TIMEOUT') from None
        except httpx.RequestError:
            raise ExtensionError('EXTENSION_NETWORK_ERROR') from None
        except ExtensionError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError,zlib.error):
            raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None


def decode_json(body):
    try:
        return json.loads(body.decode('utf-8'), parse_float=str, parse_constant=_constant, object_pairs_hook=_unique)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ExtensionError('EXTENSION_RESPONSE_INVALID') from None
