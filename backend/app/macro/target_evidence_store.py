"""仅追加证据及0HTTP重放；无供应商执行、证券主数据或发布操作。"""
from datetime import UTC, date, datetime
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.connectors.tushare.contracts import TushareResponse
from app.db.models.enterprise_target_evidence import TargetObservation, TargetCandidate
from app.macro import target_evidence_normalize as normalizer
from app.macro.target_evidence_contract import (
    TargetEvidenceRequest, canonical_json, evidence_hash, target_request_hash, target_request_payload,
)
from app.macro.target_evidence_policy import TargetEvidencePolicy, target_evidence_policy_hash
from app.macro.target_evidence_schema import target_evidence_schema


def _instant(value):
    if not isinstance(value, datetime): raise ValueError('TARGET_EVIDENCE_INVALID')
    if value.utcoffset() is None: raise ValueError('TARGET_EVIDENCE_INVALID')
    return value.astimezone(UTC)


def _observation(request, response, captured_at):
    if any(type(value) not in (str, int, float, bool, type(None)) for row in response.items for value in row):
        raise ValueError('TARGET_EVIDENCE_INVALID')
    body = normalizer.response_payload(response)
    return {'provider': 'tushare', **target_request_payload(request),
        'requestHash': target_request_hash(request), 'response': body, 'responseHash': evidence_hash(body),
        'capturedAt': _instant(captured_at).isoformat()}


def _context(observation_id, version, policy_hash, as_of, timezone):
    return {'observationId': observation_id, 'normalizerVersion': version,
        'policyHash': policy_hash, 'asOf': as_of.isoformat(), 'timezone': timezone}


def _decode_raw(row):
    try:
        if row is None or row.provider != 'tushare' or set(row.response) != {'fields', 'items'}:
            raise ValueError
        request = TargetEvidenceRequest(row.target_code, row.target_kind, row.api_name, row.params, tuple(row.requested_fields))
        if (not isinstance(row.requested_fields, list) or not isinstance(row.response['fields'], list)
            or not isinstance(row.response['items'], list)
            or any(not isinstance(item, list) for item in row.response['items'])):
            raise ValueError
        # JSON数组恢复tuple，不对任何业务标量做类型强转。
        response = TushareResponse.model_validate({'fields': tuple(row.response['fields']),
            'items': tuple(tuple(item) for item in row.response['items'])}, strict=True)
        if (len(set(response.fields)) != len(response.fields) or set(response.fields) != set(request.fields)
            or any(len(item) != len(response.fields) for item in response.items)):
            raise ValueError
        captured = _instant(row.captured_at)
        value = _observation(request, response, captured)
        if (row.request_hash != value['requestHash'] or row.response_hash != value['responseHash']
            or row.observation_id != evidence_hash(value)):
            raise ValueError
        return request, response, captured, value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('TARGET_EVIDENCE_INVALID') from None


def _check_candidate(row, observations):
    try:
        context = _context(row.observation_id, row.normalizer_version, row.policy_hash, row.as_of, row.timezone)
        if (row.observation_id not in observations or row.candidate_id != evidence_hash(context)
            or row.payload_hash != evidence_hash(row.payload)
            or row.payload['contractVersion'] != row.normalizer_version
            or row.payload['asOf'] != row.as_of.isoformat() or row.payload['timezone'] != row.timezone
            or row.payload['responseHash'] != observations[row.observation_id][3]['responseHash']
            or row.payload['requestHash'] != observations[row.observation_id][3]['requestHash']
            or row.payload['capturedAt'] != observations[row.observation_id][3]['capturedAt']
            or row.payload['completeness'] != 'not_proven' or row.payload['sourceConstituentCount'] is not None
            or row.payload['membershipReady'] is not False):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('TARGET_EVIDENCE_INVALID') from None


class TargetEvidenceStore:
    def __init__(self, factory):
        self.factory = factory

    def _gate(self):
        if target_evidence_schema(self.factory): raise ValueError('TARGET_EVIDENCE_SCHEMA_REQUIRED')

    def receive(self, request: TargetEvidenceRequest, response: TushareResponse,
        captured_at: datetime, policy: TargetEvidencePolicy) -> str:
        p = normalizer.validated_policy(policy)
        normalizer.validate_outer(request, response, p)
        value = _observation(request, response, captured_at)
        if len(canonical_json(value['response'])) > p.max_response_bytes:
            raise ValueError('TARGET_EVIDENCE_INVALID')
        self._gate()
        key = evidence_hash(value)
        def existing():
            with self.factory() as session:
                row = session.get(TargetObservation, key)
                if row is None: return False
                decoded = _decode_raw(row)
                if decoded[3] != value: raise ValueError('TARGET_EVIDENCE_CONFLICT')
                return True
        if existing(): return key
        try:
            with self.factory.begin() as session:
                session.add(TargetObservation(observation_id=key, provider='tushare', target_code=request.target_code,
                    target_kind=request.target_kind, api_name=request.api_name, params=dict(request.params),
                    requested_fields=list(request.fields), request_hash=value['requestHash'], response=value['response'],
                    response_hash=value['responseHash'], captured_at=_instant(captured_at)))
        except IntegrityError:
            if not existing(): raise ValueError('TARGET_EVIDENCE_CONFLICT') from None
        return key

    def normalize(self, observation_id: str, policy: TargetEvidencePolicy, as_of: date,
        timezone: str, *, persist: bool = False) -> dict[str, object]:
        p = normalizer.validated_policy(policy)
        if not isinstance(observation_id, str) or re.fullmatch('[0-9a-f]{64}', observation_id) is None or type(persist) is not bool:
            raise ValueError('TARGET_EVIDENCE_INVALID')
        self._gate()
        # 未完成/孤儿/身份篡改不通过另写一条候选来掩盖。
        self.inventory()
        with self.factory() as session:
            decoded = _decode_raw(session.get(TargetObservation, observation_id))
            request, response, captured, raw = decoded
            normalizer.validate_outer(request, response, p)
            if len(canonical_json(raw['response'])) > p.max_response_bytes: raise ValueError('TARGET_EVIDENCE_INVALID')
            value = normalizer.normalize_target_response(request, response, captured, as_of, p, timezone)
            context = _context(observation_id, normalizer.NORMALIZER_VERSION, target_evidence_policy_hash(p), as_of, timezone)
            key = evidence_hash(context)
            existing = session.get(TargetCandidate, key)
            if existing is not None:
                _check_candidate(existing, {observation_id: decoded})
                if existing.payload != value: raise ValueError('TARGET_EVIDENCE_CONFLICT')
                return value
        if persist:
            try:
                with self.factory.begin() as session:
                    session.add(TargetCandidate(candidate_id=key, observation_id=observation_id,
                        normalizer_version=normalizer.NORMALIZER_VERSION, policy_hash=context['policyHash'],
                        as_of=as_of, timezone=timezone, payload=value, payload_hash=evidence_hash(value)))
            except IntegrityError:
                with self.factory() as session:
                    row = session.get(TargetCandidate, key)
                    if row is None: raise ValueError('TARGET_EVIDENCE_CONFLICT') from None
                    _check_candidate(row, {observation_id: decoded})
                    if row.payload != value: raise ValueError('TARGET_EVIDENCE_CONFLICT') from None
        return value

    def inventory(self) -> dict[str, object]:
        self._gate()
        with self.factory() as session:
            observations = {r.observation_id: _decode_raw(r)
                for r in session.scalars(select(TargetObservation))}
            candidates = list(session.scalars(select(TargetCandidate)))
            for row in candidates: _check_candidate(row, observations)
        return {'observationCount': len(observations), 'candidateCount': len(candidates)}
