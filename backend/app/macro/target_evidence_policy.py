"""只描述候选证据的资源策略；参数存在不代表采集/物理预算已实现。"""
from dataclasses import dataclass
from hashlib import sha256
import json
import math


_FIELDS = {
    'collectionEnabled': 'collection_enabled', 'useScope': 'use_scope',
    'bootstrapTargetCodes': 'bootstrap_target_codes', 'maxTargetsPerPlan': 'max_targets_per_plan',
    'maxHttpPerRun': 'max_http_per_run', 'maxHttpPerPlan': 'max_http_per_plan',
    'requestsPerMinute': 'requests_per_minute', 'requestTimeoutSeconds': 'request_timeout_seconds',
    'maxRetries': 'max_retries', 'lookbackMonths': 'lookback_months',
    'indexMetadataMarket': 'index_metadata_market', 'maxResponseBytes': 'max_response_bytes',
    'maxResponseRows': 'max_response_rows', 'maxSnapshotMembers': 'max_snapshot_members',
}


@dataclass(frozen=True, slots=True)
class TargetEvidencePolicy:
    collection_enabled: bool
    use_scope: str
    bootstrap_target_codes: tuple[str, ...]
    max_targets_per_plan: int
    max_http_per_run: int
    max_http_per_plan: int
    requests_per_minute: int
    request_timeout_seconds: float
    max_retries: int
    lookback_months: int
    index_metadata_market: str
    max_response_bytes: int
    max_response_rows: int
    max_snapshot_members: int


def target_evidence_policy_payload(policy: TargetEvidencePolicy) -> dict[str, object]:
    return {key: list(policy.bootstrap_target_codes) if key == 'bootstrapTargetCodes'
        else getattr(policy, name) for key, name in _FIELDS.items()}


def load_target_evidence_policy(value: object, target_codes: tuple[str, ...]) -> TargetEvidencePolicy:
    if not isinstance(value, dict) or set(value) != set(_FIELDS):
        raise ValueError('TARGET_EVIDENCE_POLICY_INVALID')
    integers = ('maxTargetsPerPlan', 'maxHttpPerRun', 'maxHttpPerPlan', 'requestsPerMinute',
        'lookbackMonths', 'maxResponseBytes', 'maxResponseRows', 'maxSnapshotMembers')
    codes = value['bootstrapTargetCodes']
    if (any(type(value[k]) is not int or value[k] <= 0 for k in integers)
        or type(value['collectionEnabled']) is not bool
        or value['useScope'] != 'local-development-only' or value['indexMetadataMarket'] != 'CSI'
        or type(value['maxRetries']) is not int or value['maxRetries'] != 0
        or not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes)
        or len(set(codes)) != len(codes) or not set(codes) <= set(target_codes)
        or len(codes) > value['maxTargetsPerPlan'] or value['maxHttpPerRun'] > value['maxHttpPerPlan']):
        raise ValueError('TARGET_EVIDENCE_POLICY_INVALID')
    timeout = value['requestTimeoutSeconds']
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(float(timeout)) and timeout > 0
    except (ValueError, OverflowError):
        valid_timeout = False
    if not valid_timeout:
        raise ValueError('TARGET_EVIDENCE_POLICY_INVALID')
    values = {_FIELDS[k]: v for k, v in value.items()}
    values['bootstrap_target_codes'] = tuple(codes)
    values['request_timeout_seconds'] = float(timeout)
    return TargetEvidencePolicy(**values)


def target_evidence_policy_hash(policy: TargetEvidencePolicy) -> str:
    return sha256(json.dumps(target_evidence_policy_payload(policy), ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
