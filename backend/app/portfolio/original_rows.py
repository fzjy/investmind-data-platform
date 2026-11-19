"""从绑定的 HTTP 原字节恢复精确数值并核对规范化结果。"""
import base64
from decimal import Decimal
from hashlib import sha256
import json


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('HTTP正文含重复键')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('HTTP正文含非有限数值')


def _original_rows(raw, fields, *, allow_more=False):
    payload = raw.payload
    http = payload['_http']
    if type(http.get('statusCode')) is not int or http['statusCode'] != 200 or http.get('credentialsRedacted') is not False:
        raise ValueError('HTTP状态或脱敏证据不合格')
    body = base64.b64decode(http['bodyBase64'], validate=True)
    if type(http.get('byteLength')) is not int or len(body) != http['byteLength'] or sha256(body).hexdigest() != http.get('bodySha256'):
        raise ValueError('HTTP字节摘要或长度不匹配')
    decoded = json.loads(body, object_pairs_hook=_object, parse_constant=_constant)
    exact = json.loads(body, parse_float=Decimal, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(decoded, dict) or type(decoded.get('code')) is not int or decoded['code'] != 0:
        raise ValueError('HTTP业务状态不成功')
    data = decoded.get('data')
    if not isinstance(data, dict) or data.get('fields') != fields or not isinstance(data.get('items'), list):
        raise ValueError('HTTP字段合同不匹配')
    if 'has_more' in data and (type(data['has_more']) is not bool or (data['has_more'] and not allow_more)):
        raise ValueError('HTTP分页未完成或状态无效')
    # count是供应商信封元数据，未约定等于items行数，不能臆造该等式。
    # 仅消费已验证的事实块，historyComplete始终为False。
    # 序列化比对保留标量类型区别（False不可冒充0），同时允许正常JSON浮点规范化。
    normalized = {key:payload[key] for key in ('fields', 'items')}
    facts = {key:data[key] for key in ('fields', 'items')}
    if json.dumps(facts, sort_keys=True, allow_nan=False) != json.dumps(normalized, sort_keys=True, allow_nan=False):
        raise ValueError('HTTP正文与规范块不匹配')
    result = []
    for index, values in enumerate(exact['data']['items'], 1):
        if not isinstance(values, list) or len(values) != len(fields):
            raise ValueError('HTTP行宽不匹配')
        result.append(dict(zip(fields, values, strict=True)) |
                      {'sourceId':f'raw.source_payload:{raw.id}:row:{index}'})
    return result
