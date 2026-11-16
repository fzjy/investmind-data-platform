import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.acquisition.contracts import Attempt, Evidence, StateKey, utc
from app.db.models.acquisition import DataAcquisitionState as State

KEY_FIELDS = tuple(StateKey.__dataclass_fields__)


def validate_metadata(value):
    if isinstance(value,dict):
        for key, item in value.items():
            if not isinstance(key,str) or re.search(r'token|secret|password|authorization|credential|url|payload|body',key,re.I):
                raise ValueError('凭证包含不应公开的字段')
            validate_metadata(item)
    elif isinstance(value,(tuple,list)):
        for item in value:
            validate_metadata(item)
    elif isinstance(value,str) and (len(value)>512 or '://' in value or any(ord(c)<32 for c in value)):
        raise ValueError('凭证包含不应公开的文本')


def scope_id(scope):
    if 'coverageContract' in scope:
        identity = {'coverageContract':scope['coverageContract'],'source':scope.get('source')}
    elif 'seriesId' in scope and 'contract' in scope:
        identity = {k:scope[k] for k in ('seriesId','contract')}
    else:
        identity = {k:v for k,v in scope.items() if k not in ('runId','confirmedEmpty','rawAt','checkKind','batchId')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def serialize(value):
    return {**asdict(value), 'checked_at': utc(value.checked_at).isoformat() if value.checked_at else None,
            'success_at': utc(value.success_at).isoformat() if value.success_at else None}


def deserialize(value):
    return Evidence(**{**value, 'checked_at':datetime.fromisoformat(value['checked_at']) if value['checked_at'] else None,
                       'success_at':datetime.fromisoformat(value['success_at']) if value['success_at'] else None,
                       'references':tuple(value['references'])})


class StateRepository:
    def __init__(self, session: Session):
        self.session = session

    def _locked(self, key):
        return self.session.scalar(select(State).filter_by(**asdict(key)).with_for_update()
                                   .execution_options(populate_existing=True))

    def begin(self, attempt: Attempt) -> bool:
        validate_metadata(attempt.scope)
        if attempt.started_at.utcoffset() is None:
            raise ValueError('尝试开始时间缺少时区')
        values = dict(attempt_id=attempt.attempt_id, started_at=utc(attempt.started_at), scope=attempt.scope,
                      active=True, trigger=attempt.trigger)
        self.session.execute(insert(State).values(**asdict(attempt.key), **values, terminal={},
                                                last_success={}, failures={}).on_conflict_do_nothing(index_elements=KEY_FIELDS))
        row = self._locked(attempt.key)
        if row.attempt_id == attempt.attempt_id:
            return row.active and row.scope == attempt.scope and utc(row.started_at) == attempt.started_at
        if (attempt.started_at, attempt.attempt_id) <= (utc(row.started_at), row.attempt_id):
            return False
        for key, value in values.items():
            setattr(row, key, value)
        self.session.flush()
        return True

    def finish(self, attempt: Attempt, evidence: Evidence) -> bool:
        validate_metadata(evidence.scope)
        validate_metadata(evidence.references)
        validate_metadata(evidence.reason)
        if evidence.status not in ('success','failed','unrecorded'):
            raise ValueError('无效终态')
        row = self._locked(attempt.key)
        if row is None or not row.active or row.attempt_id != attempt.attempt_id or row.scope != attempt.scope:
            return False
        if evidence.status in ('success','failed') and scope_id(evidence.scope) != scope_id(attempt.scope):
            raise ValueError('终态证据与冻结请求范围不匹配')
        if evidence.checked_at and evidence.checked_at < attempt.started_at:
            raise ValueError('完成时间早于开始时间')
        result = serialize(evidence)
        token = scope_id(attempt.scope)
        failures = dict(row.failures)
        if evidence.status == 'failed':
            failures[token] = result
        if evidence.status == 'success':
            failures.pop(token, None)
            row.last_success = result
        row.active, row.terminal, row.failures = False, result, failures
        self.session.flush()
        return True

    def successes(self,keys):
        if not keys:
            return {}
        columns = [getattr(State,k) for k in KEY_FIELDS]
        stmt = select(*columns,State.last_success).where(tuple_(*columns).in_(
            [tuple(asdict(key).values()) for key in keys]))
        return {StateKey(*row[:-1]):deserialize(row[-1]) for row in self.session.execute(stmt) if row[-1]}

    def read(self, keys: tuple[StateKey,...]) -> dict[StateKey,Evidence]:
        if not keys:
            return {}
        columns = [getattr(State, k) for k in KEY_FIELDS]
        rows = self.session.scalars(select(State).where(tuple_(*columns).in_(
            [tuple(asdict(key).values()) for key in keys])))
        results = {}
        for row in rows:
            key = StateKey(*(getattr(row, k) for k in KEY_FIELDS))
            if row.active:
                success = row.last_success.get('success_at')
                results[key] = Evidence('running', utc(row.started_at), datetime.fromisoformat(success) if success else None,
                                        reason='正在执行', scope=row.scope)
                continue
            terminal = max(row.failures.values(), key=lambda v:v['checked_at']) if row.failures else row.terminal
            terminal = dict(terminal)
            success = row.last_success.get('success_at')
            if success and terminal.get('checked_at') and success <= terminal['checked_at']:
                terminal['success_at'] = success
            results[key] = deserialize(terminal)
        return results
