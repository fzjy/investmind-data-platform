"""深市替代接口的单次研究原件；不声明单位、完整性或Core可用。"""
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.macro.extensions.policy_types import StrictModel

FIELDS = ['trade_date', 'ts_code', 'count', 'float_mv']


class SzSampleLimits(StrictModel):
    maxTotalHttp: int = Field(ge=1, le=1)
    maxHttpPerRun: int = Field(ge=1, le=1)
    requestsPerMinute: int = Field(ge=1, le=1)
    requestTimeoutSeconds: int = Field(ge=1, le=20)
    maxRetries: int = Field(ge=0, le=0)
    maxResponseBytes: int = Field(ge=1, le=1048576)
    maxResponseRows: int = Field(ge=1, le=64)


class SzMarketSamplePolicy(StrictModel):
    enabled: bool
    parentRunId: str
    providerUrl: Literal['https://api.tushare.pro']
    documentationUrl: Literal['https://tushare.pro/document/2?doc_id=268']
    apiName: Literal['sz_daily_info']
    fields: list[str]
    unitStatus: Literal['unverified']
    useScope: Literal['personal-local-noncommercial']
    limits: SzSampleLimits

    @model_validator(mode='after')
    def closed_identity(self):
        if (str(UUID(self.parentRunId)) != self.parentRunId or len(self.fields) != 4
            or set(self.fields) != set(FIELDS)):
            raise ValueError('SZ_SAMPLE_POLICY_INVALID')
        return self


def assemble_sample_plan(policy, parent, target):
    from app.macro.extensions.plan import digest
    if (type(target) is not str or date.fromisoformat(target).isoformat() != target
        or type(parent) is not dict or set(parent) != {'runId', 'planFingerprint', 'businessSha256'}
        or parent['runId'] != policy.parentRunId
        or any(type(parent[k]) is not str or len(parent[k]) != 64
            or any(c not in '0123456789abcdef' for c in parent[k]) for k in ('planFingerprint', 'businessSha256'))):
        raise ValueError('SZ_SAMPLE_PARENT_INVALID')
    value = {'schemaVersion': 1, 'scope': 'sz-market-contract-pilot',
        'policy': policy.model_dump(exclude={'enabled'}), 'parent': parent, 'targetDate': target,
        'query': {'api': policy.apiName, 'params': {'trade_date': target.replace('-', '')}, 'fields': policy.fields},
        'limits': policy.limits.model_dump(), 'unitVerified': False, 'coreEligible': False}
    return {**value, 'fingerprint': digest(value)}


def validate_sample_plan(plan):
    from app.macro.extensions.plan import canonical
    try:
        p = SzMarketSamplePolicy.model_validate({**plan['policy'], 'enabled': False})
        expected = assemble_sample_plan(p, plan['parent'], plan['targetDate'])
        if canonical(expected) != canonical(plan):
            raise ValueError()
        return p
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('SZ_SAMPLE_PLAN_INVALID') from None


def build_sample_plan(factory, policy, now):
    from app.db.models.ops import JobRun
    from app.macro.exchange_summary_store import ExchangeSummaryStore
    from app.macro.extensions.plan import digest
    from app.macro.extensions.store import instant
    from app.macro.extensions.ingest import utc
    with factory() as session:
        row = session.get(JobRun, policy.parentRunId)
        d = ExchangeSummaryStore(factory)._run(session, row)
        p, doc = d['plan'], d['document']
        if (p['groupId'] != 'aShareFloatMv' or doc['query']['params'].get('ts_code') != 'SZ_A'
            or doc['query']['params'].get('exchange') != 'SZ' or d['phase'] != 'completed'
            or d['normalized'] != {'source': 'tushare', 'status': 'empty', 'points': []}
            or d['coreBindings'] or utc(row.finished_at) > instant(now)):
            raise ValueError('SZ_SAMPLE_PARENT_INVALID')
        data, _ = ExchangeSummaryStore(factory)._raw(session, row)
        return assemble_sample_plan(policy, {'runId': row.id, 'planFingerprint': p['fingerprint'],
            'businessSha256': digest(data)}, p['targetDate'])


def safe_sample(data, plan):
    from app.macro.extensions.plan import canonical
    validate_sample_plan(plan)
    if type(data) is not dict or set(data) != {'fields', 'items'}:
        raise ValueError('SZ_SAMPLE_RAW_INVALID')
    fields, rows = data['fields'], data['items']
    if (type(fields) is not list or len(fields) != 4 or any(type(f) is not str for f in fields)
        or set(fields) != set(FIELDS) or type(rows) is not list or len(rows) > plan['limits']['maxResponseRows']
        or any(type(row) is not list or len(row) != 4 for row in rows)
        or any(type(v) not in (str, int, type(None)) or type(v) is str and len(v) > 128 for row in rows for v in row)
        or len(canonical(data).encode()) > plan['limits']['maxResponseBytes']):
        raise ValueError('SZ_SAMPLE_RAW_INVALID')
    return {'fields': list(fields), 'items': [[str(v) if type(v) is int and fields[i] == 'float_mv' else v
        for i, v in enumerate(row)] for row in rows]}


def sample_summary(data, plan):
    data = safe_sample(data, plan)
    rows = [dict(zip(data['fields'], row, strict=True)) for row in data['items']]
    codes = [r['ts_code'] for r in rows if type(r['ts_code']) is str]
    known = {'股票', '主板A股', '主板B股', '主板CDR', '创业板A股', '创业板CDR', '中小板',
        '基金', 'ETF', 'LOF', '封闭式基金', '封闭基金', '分级基金', '基础设施基金', '债券', '债券现券', '债券回购', 'ABS', '期权', '优先股'}
    return {'rowCount': len(rows), 'sameDate': bool(rows) and all(r['trade_date'] == plan['query']['params']['trade_date'] for r in rows),
        'aShareRowsPresent': all(codes.count(key) == 1 for key in ('主板A股', '创业板A股')),
        'duplicateCategories': len(set(codes)) != len(codes), 'knownCategories': sorted(set(codes) & known),
        'unknownCategoryCount': sum(r['ts_code'] not in known if type(r['ts_code']) is str else True for r in rows),
        'unitVerified': False, 'coreEligible': False, 'coverageVerified': False}
