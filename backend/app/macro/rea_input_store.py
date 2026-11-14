"""冻结计划/原响应/逐字段证据仓库；正式PG表不可变，恢复不盲重发。"""
from dataclasses import asdict
from contextlib import contextmanager
from datetime import UTC, date, datetime
import json
import re

from sqlalchemy import func, select
from app.connectors.tushare.contracts import TushareResponse
from app.db.models.enterprise_rea_inputs import ReaInputPlan, ReaInputAttempt, ReaInputResponse, ReaInputSupplement
from app.macro.enterprise import FinancialRecord, _hash
from app.macro.rea_input_preview import CONTRACT, rea_input_request
from app.macro.rea_input_match import match_rea_inputs

WITNESS_VERSION = 'enterprise-rea-inputs-witness-v1'
EVIDENCE_ERRORS = frozenset({'REA_EVIDENCE_INVALID', 'REA_PAGE_POLICY_CHANGED', 'REA_ATTEMPT_UNCERTAIN', 'REA_ATTEMPT_FAILED', 'REA_FROZEN_PLAN_CHANGED'})


def _json(value):
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def _decode_snapshot(row):
    try:
        snapshot = row.snapshot
        plan = snapshot['plan']
        if (row.snapshot_hash != _hash(snapshot) or row.witness_version != WITNESS_VERSION
                or plan['contractVersion'] != CONTRACT or plan['planFingerprint'] != row.fingerprint
                or type(snapshot['pageSize']) is not int or not 1 <= snapshot['pageSize'] <= 5000):
            raise ValueError()
        sources = {}
        for key, record in snapshot['sources'].items():
            values = dict(record)
            for field in ('end_date', 'ann_date', 'f_ann_date'):
                values[field] = date.fromisoformat(values[field]) if values[field] else None
            source = FinancialRecord(**values)
            if key != source.source_key:
                raise ValueError()
            sources[key] = source
        keys = []
        for request in plan['requests']:
            source = sources[request['coreBinding']['sourceKey']]
            if request != rea_input_request(source, plan['poolVersion']):
                raise ValueError()
            keys.append(request['requestKey'])
        if len(keys) != len(set(keys)) or set(sources) != {r['coreBinding']['sourceKey'] for r in plan['requests']}:
            raise ValueError()
        return {'plan': plan, 'sources': sources, 'pageSize': snapshot['pageSize'],
                'requestsByKey': {request['requestKey']: request for request in plan['requests']}}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('REA_EVIDENCE_INVALID') from None


class ReaInputStore:
    def __init__(self, factory):
        self.factory = factory
        self._plan_cache = None

    @contextmanager
    def validation_pass(self):
        """Only reuse immutable plans within one pass; never cache Raw/Core outcomes.

        A fresh pass/store re-reads and validates the complete snapshot. PG guards
        prohibit in-pass mutations; direct recover() calls remain uncached.
        """
        previous = self._plan_cache
        self._plan_cache = {}
        try:
            yield self
        finally:
            self._plan_cache = previous

    def _snapshot(self, session, fingerprint):
        if self._plan_cache is not None and fingerprint in self._plan_cache:
            return self._plan_cache[fingerprint]
        snapshot = _decode_snapshot(session.get(ReaInputPlan, fingerprint))
        if self._plan_cache is not None:
            self._plan_cache[fingerprint] = snapshot
        return snapshot

    def latest(self):
        with self.factory() as session:
            row = session.scalar(select(ReaInputPlan).order_by(ReaInputPlan.sequence_number.desc()))
            return _decode_snapshot(row) if row else None

    def freeze(self, plan, inputs, page_size):
        all_sources = {r.source_key: r for r in inputs.financials}
        if len(all_sources) != len(inputs.financials):
            raise ValueError('REA_EVIDENCE_INVALID')
        try:
            snapshot = _json({'plan': plan, 'pageSize': page_size,
                'sources': {r['coreBinding']['sourceKey']: asdict(all_sources[r['coreBinding']['sourceKey']]) for r in plan['requests']}})
            row = ReaInputPlan(fingerprint=plan['planFingerprint'], snapshot=snapshot,
                snapshot_hash=_hash(snapshot), witness_version=WITNESS_VERSION)
            _decode_snapshot(row)
        except (ValueError, TypeError, KeyError):
            raise ValueError('REA_EVIDENCE_INVALID') from None
        with self.factory.begin() as session:
            existing = session.get(ReaInputPlan, row.fingerprint)
            if existing:
                decoded = _decode_snapshot(existing)
                # asOfDate is a frozen observation date, not part of the plan identity.
                comparable = _json(snapshot)
                comparable['plan']['asOfDate'] = existing.snapshot['plan']['asOfDate']
                if existing.snapshot != comparable:
                    raise ValueError('REA_FROZEN_PLAN_CHANGED')
                return decoded
            # Runtime holds the independent job lock. Uniqueness also rejects a racing
            # direct caller; timestamps/fingerprints never decide which plan is pending.
            row.sequence_number = (session.scalar(select(func.max(ReaInputPlan.sequence_number))) or 0) + 1
            session.add(row)
        return _decode_snapshot(row)

    def reserve(self, request, fingerprint, run_id, page_size):
        with self.factory.begin() as session:
            snapshot = self._snapshot(session, fingerprint)
            if snapshot['pageSize'] != page_size:
                raise ValueError('REA_PAGE_POLICY_CHANGED')
            if snapshot['requestsByKey'].get(request['requestKey']) != request:
                raise ValueError('REA_EVIDENCE_INVALID')
            session.add(ReaInputAttempt(request_key=request['requestKey'], plan_fingerprint=fingerprint,
                run_id=run_id, request=_json(request), page_size=page_size, state='reserved'))
        return request['requestKey']

    def receive(self, request_key, response):
        payload = _json(response.model_dump(mode='json'))
        with self.factory.begin() as session:
            attempt = session.get(ReaInputAttempt, request_key)
            if attempt is None or attempt.state != 'reserved':
                raise ValueError('REA_EVIDENCE_INVALID')
            session.add(ReaInputResponse(request_key=request_key, payload=payload, response_hash=_hash(payload)))
            attempt.state = 'received'
            attempt.finished_at = datetime.now(UTC)

    def fail(self, request_key, error_code):
        if not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', error_code):
            raise ValueError('REA_EVIDENCE_INVALID')
        with self.factory.begin() as session:
            attempt = session.get(ReaInputAttempt, request_key)
            if attempt is None or attempt.state != 'reserved':
                raise ValueError('REA_EVIDENCE_INVALID')
            attempt.state = 'failed'
            attempt.error_code = error_code
            attempt.finished_at = datetime.now(UTC)

    def result_exists(self, request_key):
        with self.factory() as session:
            return session.get(ReaInputSupplement, request_key) is not None

    def recover(self, request, source, page_size, *, persist=False):
        key = request['requestKey']
        with self.factory() as session:
            attempt = session.get(ReaInputAttempt, key)
            response = session.get(ReaInputResponse, key)
            result = session.get(ReaInputSupplement, key)
            if attempt is None:
                if response is not None or result is not None:
                    raise ValueError('REA_EVIDENCE_INVALID')
                return None
            snapshot = self._snapshot(session, attempt.plan_fingerprint)
            if attempt.page_size != page_size or snapshot['pageSize'] != page_size:
                raise ValueError('REA_PAGE_POLICY_CHANGED')
            if attempt.request != request or snapshot['requestsByKey'].get(key) != request:
                raise ValueError('REA_EVIDENCE_INVALID')
            if response is None:
                if result is not None or attempt.state == 'received':
                    raise ValueError('REA_EVIDENCE_INVALID')
                raise ValueError('REA_ATTEMPT_UNCERTAIN' if attempt.state == 'reserved' else 'REA_ATTEMPT_FAILED')
            if attempt.state != 'received' or attempt.finished_at is None or response.response_hash != _hash(response.payload):
                raise ValueError('REA_EVIDENCE_INVALID')
            try:
                outcome = match_rea_inputs(request, source, TushareResponse.model_validate(response.payload), page_size)
            except (ValueError, KeyError, TypeError):
                raise ValueError('REA_EVIDENCE_INVALID') from None
            if result is not None and (result.core_binding != request['coreBinding']
                    or result.witness_version != WITNESS_VERSION or result.status != outcome['status'] or result.outcome != outcome):
                raise ValueError('REA_EVIDENCE_INVALID')
        if result is None and persist:
            with self.factory.begin() as session:
                session.add(ReaInputSupplement(request_key=key, core_binding=request['coreBinding'],
                    witness_version=WITNESS_VERSION, status=outcome['status'], outcome=outcome))
        return outcome
