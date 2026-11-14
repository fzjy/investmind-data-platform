"""独立补充证据仓库：持久响应可重放，既存结果不可覆盖。"""
from datetime import UTC, datetime
import json
from uuid import uuid4

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.db.models.enterprise_receivables import ReceivableAttempt, ReceivableResponse, ReceivableSupplement
from app.macro.receivables_match import match_receivable
from app.macro.receivables_preview import _digest
from app.macro.balance_supplement import RECEIVABLES, target_options

WITNESS_VERSION = 'enterprise-receivables-witness-v1'
EVIDENCE_ERRORS = frozenset({'ENTERPRISE_EVIDENCE_INVALID', 'ENTERPRISE_PAGE_POLICY_CHANGED',
    'ENTERPRISE_SOURCE_BINDING_INVALID'})


class ReceivablesStore:
    def __init__(self, factory, *, target=RECEIVABLES):
        self.options=target_options(target)
        self.Attempt,self.Response,self.Supplement=target.models
        self.witness=target.witness
        self.factory = factory

    def reserve(self, request: dict, fingerprint: str, run_id: str, page_size: int) -> str:
        attempt_id = str(uuid4())
        # 不将token或传输地址交给仓库；请求仅来自纯规划。
        payload = json.loads(json.dumps(request, allow_nan=False))
        with self.factory.begin() as session:
            session.add(self.Attempt(id=attempt_id, run_id=run_id,
                request_key=request['requestKey'], plan_fingerprint=fingerprint,
                request=payload, page_size=page_size, state='reserved'))
        return attempt_id

    def receive(self, attempt_id: str, response: TushareResponse) -> None:
        payload = json.loads(json.dumps(response.model_dump(mode='json'), allow_nan=False))
        with self.factory.begin() as session:
            attempt = session.get(self.Attempt, attempt_id)
            if attempt is None or attempt.state != 'reserved':
                raise ValueError('ENTERPRISE_EVIDENCE_INVALID')
            session.add(self.Response(attempt_id=attempt_id, request_key=attempt.request_key,
                payload=payload, response_hash=_digest(payload)))
            attempt.state = 'received'
            attempt.finished_at = datetime.now(UTC)

    def fail(self, attempt_id: str, error_code: str) -> None:
        with self.factory.begin() as session:
            attempt = session.get(self.Attempt, attempt_id)
            if attempt is None or attempt.state != 'reserved':
                raise ValueError('ENTERPRISE_EVIDENCE_INVALID')
            attempt.state = 'failed'
            attempt.error_code = error_code
            attempt.finished_at = datetime.now(UTC)

    def save_result(self, request: dict, response_id: str, outcome: dict) -> None:
        with self.factory.begin() as session:
            session.add(self.Supplement(request_key=request['requestKey'], response_id=response_id,
                core_binding=request['coreBinding'], witness_version=self.witness,
                status=outcome['status'], value=outcome['value']))

    def recover(self, request: dict, source, page_size: int, *, persist: bool = False) -> dict | None:
        with self.factory() as session:
            result = session.get(self.Supplement, request['requestKey'])
            if result is not None:
                response = session.get(self.Response, result.response_id)
            else:
                response = session.scalar(select(self.Response).join(self.Attempt,
                    self.Attempt.id == self.Response.attempt_id)
                    .where(self.Response.request_key == request['requestKey'])
                    .order_by(self.Attempt.created_at.desc(), self.Attempt.id.desc()))
            if response is None:
                if result is not None:
                    raise ValueError('ENTERPRISE_EVIDENCE_INVALID')
                return None
            attempt = session.get(self.Attempt, response.attempt_id)
            if (attempt is None or attempt.state != 'received' or attempt.request != request
                    or attempt.request_key != request['requestKey'] or response.request_key != request['requestKey']
                    or response.response_hash != _digest(response.payload)):
                raise ValueError('ENTERPRISE_EVIDENCE_INVALID')
            if attempt.page_size != page_size:
                raise ValueError('ENTERPRISE_PAGE_POLICY_CHANGED')
            try:
                decoded = TushareResponse.model_validate(response.payload)
                outcome = match_receivable(request, source, decoded, page_size, **self.options)
            except ValueError:
                raise ValueError('ENTERPRISE_EVIDENCE_INVALID') from None
            response_id = response.attempt_id
            if result is not None and (result.core_binding != request['coreBinding']
                    or result.witness_version != self.witness
                    or result.status != outcome['status'] or result.value != outcome['value']):
                raise ValueError('ENTERPRISE_EVIDENCE_INVALID')
        if result is None and persist:
            self.save_result(request, response_id, outcome)
        return outcome
