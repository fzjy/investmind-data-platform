"""单根预算预留及Raw/received原子提交；没有HTTP能力。"""
from datetime import UTC, date, datetime, timedelta
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.enterprise_target_execution import TargetExecutionPlan as Plan, TargetExecutionAttempt as Attempt
from app.db.models.enterprise_target_evidence import TargetObservation, TargetCandidate
from app.macro.target_evidence_contract import canonical_json, evidence_hash, target_request_payload
from app.macro.target_evidence_normalize import NORMALIZER_VERSION, validate_outer
from app.macro.target_evidence_policy import load_target_evidence_policy, target_evidence_policy_hash
from app.macro.target_evidence_store import TargetEvidenceStore, _observation, _decode_raw, _instant
from app.macro.target_execution_plan import freeze_target_plan, validate_target_plan, validate_frozen_root, derive_target_steps, execution_step, step_request
from app.macro.target_execution_schema import target_execution_schema

SLOT='bootstrap'


def _root(row):
    if row is None or row.slot!=SLOT or row.fingerprint!=evidence_hash(row.snapshot) or row.snapshot_hash!=row.fingerprint:
        raise ValueError('TARGET_EXECUTION_INVALID')
    return validate_frozen_root({'fingerprint':row.fingerprint,'snapshot':row.snapshot})


def _policy(root):
    value=root['snapshot']
    return load_target_evidence_policy(value['policy'],tuple(t['code'] for t in value['targets']))


def _stored_root(session):
    rows=session.scalars(select(Plan)).all()
    if not rows:return None
    if len(rows)!=1:raise ValueError('TARGET_EXECUTION_INVALID')
    return _root(rows[0])


class TargetExecutionStore:
    def __init__(self,factory): self.factory=factory

    def _gate(self):
        if target_execution_schema(self.factory): raise ValueError('TARGET_EXECUTION_SCHEMA_REQUIRED')

    def prepare(self,catalog,selection,as_of,timezone,*,persist=False):
        self._gate()
        with self.factory() as session:
            existing=_stored_root(session)
            if existing is not None:
                return validate_target_plan(existing,catalog,selection,timezone,as_of)
        root=freeze_target_plan(catalog,selection,as_of,timezone)
        if persist:
            try:
                with self.factory.begin() as session:
                    session.add(Plan(slot=SLOT,fingerprint=root['fingerprint'],snapshot=root['snapshot'],snapshot_hash=root['fingerprint']))
            except IntegrityError:
                with self.factory() as session:
                    existing=_stored_root(session)
                    if existing is None:raise ValueError('TARGET_EXECUTION_INVALID') from None
                    return validate_target_plan(existing,catalog,selection,timezone,as_of)
        return root

    def progress(self,root,policy=None,*,persist=False):
        self._gate(); validate_frozen_root(root); policy=policy or _policy(root)
        if target_evidence_policy_hash(policy)!=root['snapshot']['policyHash']:
            raise ValueError('TARGET_FROZEN_PLAN_CHANGED')
        evidence=TargetEvidenceStore(self.factory); evidence.inventory()
        outcomes={}; missing=[]; blocked=[]; pending=[]; count=0
        with self.factory() as session:
            stored=_stored_root(session)
            if stored is not None and stored!=root: raise ValueError('TARGET_EXECUTION_INVALID')
            rows=session.scalars(select(Attempt)).all()
            if rows and stored is None: raise ValueError('TARGET_EXECUTION_INVALID')
            for row in rows:
                step=row.request; request=step_request(step)
                if (set(step)!={'requestKey','request','parentObservationIds'}
                    or execution_step(root,request,step['parentObservationIds'])!=step
                    or row.request_key!=step['requestKey'] or row.request_hash!=evidence_hash(step)
                    or row.plan_fingerprint!=root['fingerprint'] or len(row.run_id)!=36
                    or row.state not in ('reserved','received','failed')):
                    raise ValueError('TARGET_EXECUTION_INVALID')
                count+=1
                if row.state!='received':
                    if row.observation_id is not None or (row.state=='reserved' and (row.error_code is not None or row.finished_at is not None)):
                        raise ValueError('TARGET_EXECUTION_INVALID')
                    if row.state=='failed' and (not row.error_code or row.finished_at is None): raise ValueError('TARGET_EXECUTION_INVALID')
                    blocked.append('TARGET_ATTEMPT_UNCERTAIN' if row.state=='reserved' else 'TARGET_ATTEMPT_FAILED')
                    pending.append(step)
                    continue
                if row.error_code is not None or row.finished_at is None or row.observation_id is None:
                    raise ValueError('TARGET_EXECUTION_INVALID')
                decoded=_decode_raw(session.get(TargetObservation,row.observation_id),session.get_bind().dialect.name=='sqlite')
                if target_request_payload(decoded[0])!=target_request_payload(request): raise ValueError('TARGET_EXECUTION_INVALID')
                payload=evidence.normalize(row.observation_id,policy,date.fromisoformat(root['snapshot']['frozenAsOfDate']),root['snapshot']['timezone'])
                outcomes[row.request_key]={'observationId':row.observation_id,'payload':payload}
                candidate=session.scalar(select(TargetCandidate.candidate_id).where(
                    TargetCandidate.observation_id==row.observation_id,TargetCandidate.normalizer_version==NORMALIZER_VERSION,
                    TargetCandidate.policy_hash==root['snapshot']['policyHash'],TargetCandidate.as_of==date.fromisoformat(root['snapshot']['frozenAsOfDate']),
                    TargetCandidate.timezone==root['snapshot']['timezone']))
                if candidate is None: missing.append(row.observation_id)
        if count>policy.max_http_per_plan: raise ValueError('TARGET_EXECUTION_INVALID')
        graph=derive_target_steps(root,outcomes)
        if len(pending)>1 or any(step!=graph['next'] for step in pending):
            raise ValueError('TARGET_EXECUTION_INVALID')
        if persist and not blocked:
            for observation in missing:
                evidence.normalize(observation,policy,date.fromisoformat(root['snapshot']['frozenAsOfDate']),root['snapshot']['timezone'],persist=True)
            missing=[]
        return {**graph,'rootReservedCount':count,'replayPendingCount':len(missing),
            'processingCompleted':graph['processingCompleted'] and not missing and not blocked,
            'blockedReasons':sorted(set(blocked))}

    def reserve(self,root,step,run_id,now):
        self._gate(); now=_instant(now); policy=_policy(root)
        try:
            if str(UUID(run_id))!=run_id: raise ValueError
        except (ValueError,TypeError,AttributeError): raise ValueError('TARGET_EXECUTION_INVALID') from None
        try:
            with self.factory.begin() as session:
                row=session.scalar(select(Plan).where(Plan.slot==SLOT).with_for_update())
                if _root(row)!=root: raise ValueError('TARGET_EXECUTION_INVALID')
                progress=self.progress(root)
                if progress['blockedReasons']: raise ValueError(progress['blockedReasons'][0])
                if progress['next'] is None or step!=progress['next']: raise ValueError('TARGET_EXECUTION_INVALID')
                attempts=session.scalars(select(Attempt).where(Attempt.plan_fingerprint==root['fingerprint'])).all()
                if len(attempts)>=policy.max_http_per_plan: raise ValueError('TARGET_ROOT_BUDGET_EXHAUSTED')
                if sum(a.run_id==run_id for a in attempts)>=policy.max_http_per_run: raise ValueError('TARGET_RUN_BUDGET_EXHAUSTED')
                timestamps=[_instant(a.created_at,sqlite=session.get_bind().dialect.name=='sqlite') for a in attempts]
                if any(value>now for value in timestamps): raise ValueError('TARGET_CLOCK_REGRESSION')
                if sum(value>now-timedelta(seconds=60) for value in timestamps)>=policy.requests_per_minute:
                    raise ValueError('TARGET_RATE_LIMITED')
                session.add(Attempt(request_key=step['requestKey'],plan_fingerprint=root['fingerprint'],run_id=run_id,
                    request=step,request_hash=evidence_hash(step),state='reserved',created_at=now))
            return step['requestKey']
        except IntegrityError: raise ValueError('TARGET_RESERVATION_CONFLICT') from None

    def receive(self,request_key,response,captured_at,policy):
        self._gate(); captured_at=_instant(captured_at)
        with self.factory.begin() as session:
            row=session.scalar(select(Attempt).where(Attempt.request_key==request_key).with_for_update())
            if row is None: raise ValueError('TARGET_EXECUTION_INVALID')
            plan=_stored_root(session); request=step_request(row.request)
            if plan is None:raise ValueError('TARGET_EXECUTION_INVALID')
            if (row.plan_fingerprint!=plan['fingerprint'] or row.request_hash!=evidence_hash(row.request)
                or execution_step(plan,request,row.request['parentObservationIds'])!=row.request
                or target_evidence_policy_hash(policy)!=plan['snapshot']['policyHash']): raise ValueError('TARGET_EXECUTION_INVALID')
            validate_outer(request,response,policy)
            value=_observation(request,response,captured_at)
            if len(canonical_json(value['response']))>policy.max_response_bytes: raise ValueError('TARGET_EXECUTION_INVALID')
            key=evidence_hash(value)
            if row.state=='received':
                old=session.get(TargetObservation,row.observation_id)
                if row.observation_id!=key or _decode_raw(old,session.get_bind().dialect.name=='sqlite')[3]!=value:
                    raise ValueError('TARGET_EXECUTION_INVALID')
                return
            if row.state!='reserved' or row.observation_id is not None: raise ValueError('TARGET_EXECUTION_INVALID')
            old=session.get(TargetObservation,key)
            if old is not None:
                if _decode_raw(old,session.get_bind().dialect.name=='sqlite')[3]!=value: raise ValueError('TARGET_EXECUTION_INVALID')
            else:
                session.add(TargetObservation(observation_id=key,provider='tushare',target_code=request.target_code,
                    target_kind=request.target_kind,api_name=request.api_name,params=dict(request.params),requested_fields=list(request.fields),
                    request_hash=value['requestHash'],response=value['response'],response_hash=value['responseHash'],captured_at=captured_at))
                session.flush()
            row.observation_id=key; row.state='received'; row.finished_at=captured_at

    def fail(self,request_key,error_code,now):
        self._gate(); now=_instant(now)
        if not isinstance(error_code,str) or re.fullmatch('TARGET_[A-Z0-9_]{1,56}',error_code) is None:
            raise ValueError('TARGET_EXECUTION_INVALID')
        with self.factory.begin() as session:
            row=session.scalar(select(Attempt).where(Attempt.request_key==request_key).with_for_update())
            if row is None or row.state!='reserved' or row.observation_id is not None: raise ValueError('TARGET_EXECUTION_INVALID')
            row.state='failed'; row.error_code=error_code; row.finished_at=now
