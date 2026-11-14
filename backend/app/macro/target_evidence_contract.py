"""离线证据请求身份；不构造可执行计划、不调用供应商。"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
from types import MappingProxyType

from app.connectors.tushare.contracts import Scalar


API_FIELDS = {
    'index_basic': ('ts_code', 'name', 'fullname', 'market', 'publisher', 'exp_date'),
    'etf_basic': ('ts_code', 'csname', 'index_code', 'index_name', 'exchange', 'list_status', 'etf_type'),
    'index_weight': ('index_code', 'con_code', 'trade_date', 'weight'),
}
API_PARAMS = {
    'index_basic': {'ts_code', 'market'}, 'etf_basic': {'ts_code'},
    'index_weight': {'index_code', 'start_date', 'end_date'},
}


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, OverflowError, UnicodeError):
        raise ValueError('TARGET_EVIDENCE_INVALID') from None


def evidence_hash(value: object) -> str:
    return sha256(canonical_json(value)).hexdigest()


def source_code(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value) is not None


def source_date(value: object) -> date:
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{8}', value) is None:
        raise ValueError('TARGET_EVIDENCE_INVALID')
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise ValueError('TARGET_EVIDENCE_INVALID') from None


@dataclass(frozen=True, slots=True)
class TargetEvidenceRequest:
    target_code: str
    target_kind: str
    api_name: str
    params: Mapping[str, Scalar]
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.target_code, str) or re.fullmatch(r'[0-9]{6}\.(SH|SZ)', self.target_code) is None
            or self.target_kind not in ('index', 'etf') or not isinstance(self.api_name, str)
            or self.api_name not in API_FIELDS or self.fields != API_FIELDS[self.api_name]
            or not isinstance(self.params, Mapping) or set(self.params) != API_PARAMS[self.api_name]):
            raise ValueError('TARGET_EVIDENCE_INVALID')
        params = dict(self.params)
        if self.api_name == 'etf_basic':
            valid = self.target_kind == 'etf' and params['ts_code'] == self.target_code
        elif self.api_name == 'index_basic':
            valid = source_code(params['ts_code']) and params['market'] == 'CSI'
        else:
            valid = source_code(params['index_code']) and source_date(params['start_date']) <= source_date(params['end_date'])
        if self.target_kind == 'index':
            source = params.get('ts_code') if self.api_name == 'index_basic' else params.get('index_code')
            valid = valid and source == self.target_code
        if not valid:
            raise ValueError('TARGET_EVIDENCE_INVALID')
        object.__setattr__(self, 'params', MappingProxyType(params))


def target_request_payload(request: TargetEvidenceRequest) -> dict[str, object]:
    return {'targetCode': request.target_code, 'targetKind': request.target_kind,
        'apiName': request.api_name, 'params': dict(request.params), 'fields': list(request.fields)}


def target_request_hash(request: TargetEvidenceRequest) -> str:
    return evidence_hash(target_request_payload(request))
