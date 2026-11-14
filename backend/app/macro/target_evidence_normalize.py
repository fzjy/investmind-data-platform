"""三API的纯候选校验；从不证明来源完整性或查询证券主数据。"""
from datetime import date, datetime, timezone as datetime_timezone
from decimal import Decimal
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.connectors.tushare.contracts import TushareResponse
from app.macro.target_evidence_contract import (
    TargetEvidenceRequest, evidence_hash, source_code, source_date, target_request_hash,
)
from app.macro.target_evidence_policy import (
    TargetEvidencePolicy, load_target_evidence_policy, target_evidence_policy_payload,
)


NORMALIZER_VERSION = 'enterprise-target-candidate-v1'


def validated_policy(policy: TargetEvidencePolicy) -> TargetEvidencePolicy:
    try:
        if not isinstance(policy, TargetEvidencePolicy):
            raise ValueError
        return load_target_evidence_policy(target_evidence_policy_payload(policy), policy.bootstrap_target_codes)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('TARGET_EVIDENCE_INVALID') from None


def response_payload(response: TushareResponse) -> dict[str, object]:
    return {'fields': list(response.fields), 'items': [list(row) for row in response.items]}


def validate_outer(request: TargetEvidenceRequest, response: TushareResponse, policy: TargetEvidencePolicy) -> None:
    if (not isinstance(request, TargetEvidenceRequest) or not isinstance(response, TushareResponse)
        or not isinstance(response.fields, tuple) or not all(isinstance(f, str) for f in response.fields)
        or len(set(response.fields)) != len(response.fields) or set(response.fields) != set(request.fields)
        or not isinstance(response.items, tuple) or len(response.items) > policy.max_response_rows
        or any(not isinstance(row, tuple) or len(row) != len(response.fields) for row in response.items)):
        raise ValueError('TARGET_EVIDENCE_INVALID')


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _metadata(request, rows, policy, as_of):
    if not rows:
        return 'unavailable', ['NO_METADATA'], None
    if len(rows) != 1:
        return 'rejected', ['METADATA_NOT_UNIQUE'], None
    row = rows[0]
    if row['ts_code'] != request.params['ts_code']:
        return 'rejected', ['SOURCE_IDENTITY_MISMATCH'], None
    if request.api_name == 'index_basic':
        if (not all(_text(row[k]) for k in ('name', 'fullname', 'publisher'))
            or row['market'] != policy.index_metadata_market):
            return 'rejected', ['INVALID_INDEX_METADATA'], None
        if row['exp_date'] not in (None, ''):
            try:
                expired = source_date(row['exp_date']) <= as_of
            except ValueError:
                return 'rejected', ['INVALID_EXPIRATION_DATE'], None
            if expired:
                return 'unavailable', ['INDEX_EXPIRED'], None
    else:
        if (row['exchange'] != request.target_code.split('.')[1]
            or not all(_text(row[k]) for k in ('csname', 'index_name'))
            or not source_code(row['index_code']) or row['list_status'] not in ('L', 'D', 'P')
            or row['etf_type'] not in ('境内', 'QDII')):
            return 'rejected', ['INVALID_ETF_METADATA'], None
        if row['list_status'] != 'L':
            return 'unavailable', ['ETF_NOT_LISTED'], None
        if row['etf_type'] == 'QDII':
            return 'unsupported', ['ETF_CHANNEL_UNSUPPORTED'], None
    return 'valid_candidate', [], dict(row)


def _snapshots(request, rows, policy, as_of):
    start, end = source_date(request.params['start_date']), source_date(request.params['end_date'])
    month = as_of.year * 12 + as_of.month - policy.lookback_months
    earliest = date(month // 12, month % 12 + 1, 1)
    if start < earliest or end > as_of:
        raise ValueError('TARGET_EVIDENCE_INVALID')
    if not rows:
        return 'unavailable', ['NO_MEMBERS'], []
    groups = {}
    for row in rows:
        try:
            observed = source_date(row['trade_date'])
            weight = row['weight']
            if (row['index_code'] != request.params['index_code']
                or not isinstance(row['con_code'], str)
                or re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', row['con_code']) is None
                or not start <= observed <= end or observed > as_of
                or type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 100):
                raise ValueError
            group = groups.setdefault(observed, {})
            if row['con_code'] in group or len(group) >= policy.max_snapshot_members:
                raise ValueError
            decimal = Decimal(str(weight))
            group[row['con_code']] = '0' if decimal == 0 else format(decimal.normalize(), 'f')
        except (ValueError, TypeError, OverflowError):
            return 'rejected', ['INVALID_MEMBER_ROW'], []
    snapshots = []
    for observed, members in sorted(groups.items()):
        values = [{'code': code, 'weight': weight} for code, weight in sorted(members.items())]
        snapshots.append({'indexCode': request.params['index_code'], 'tradeDate': observed.isoformat(),
            'observedMemberCount': len(values), 'members': values, 'membersHash': evidence_hash(values)})
    return 'valid_candidate', [], snapshots


def normalize_target_response(
    request: TargetEvidenceRequest, response: TushareResponse, captured_at: datetime,
    as_of: date, policy: TargetEvidencePolicy, timezone: str,
) -> dict[str, object]:
    policy = validated_policy(policy)
    try:
        if (not isinstance(captured_at, datetime) or captured_at.utcoffset() is None
            or type(as_of) is not date or not isinstance(timezone, str)
            or captured_at.astimezone(ZoneInfo(timezone)).date() < as_of):
            raise ValueError
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('TARGET_EVIDENCE_INVALID') from None
    validate_outer(request, response, policy)
    result = {'contractVersion': NORMALIZER_VERSION, 'requestHash': target_request_hash(request),
        'responseHash': None, 'capturedAt': captured_at.astimezone(datetime_timezone.utc).isoformat(),
        'asOf': as_of.isoformat(), 'timezone': timezone, 'completeness': 'not_proven',
        'sourceConstituentCount': None, 'membershipReady': False, 'notStrictPointInTime': True,
        'metadata': None, 'snapshotCandidates': []}
    # 非JSON标量无可宣称的语义摘要；不造hash，不允许该响应进入Raw存储。
    if any(type(v) not in (str, int, float, bool, type(None))
        or (type(v) is float and not math.isfinite(v)) for row in response.items for v in row):
        return dict(result, qualityStatus='rejected', reasons=['NON_JSON_SCALAR'])
    result['responseHash'] = evidence_hash(response_payload(response))
    rows = [dict(zip(response.fields, row, strict=True)) for row in response.items]
    if request.api_name == 'index_weight':
        status, reasons, snapshots = _snapshots(request, rows, policy, as_of)
        result['snapshotCandidates'] = snapshots
    else:
        if request.api_name == 'index_basic' and request.params['market'] != policy.index_metadata_market:
            raise ValueError('TARGET_EVIDENCE_INVALID')
        status, reasons, metadata = _metadata(request, rows, policy, as_of)
        result['metadata'] = metadata
    return dict(result, qualityStatus=status, reasons=reasons)
