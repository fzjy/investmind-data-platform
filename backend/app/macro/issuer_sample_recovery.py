"""仅三个已观察失败的独立恢复计划；旧采样槽不重置。"""
from copy import deepcopy
from datetime import datetime
import re
from typing import Literal
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from pydantic import model_validator
from sqlalchemy import select

from app.db.models.ops import JobRun
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import digest
from app.macro.extensions.store import instant
from app.macro.official_member_policy import StrictPolicy
from app.macro.issuer_contract_samples import SOURCES,build_sample_plan,validate_sample_plan,sample_run_key

RECOVERY_URLS={
    '512400':SOURCES['512400'][1],
    '512200':SOURCES['512200'][1],
    '512980':'https://www.gffunds.com.cn/proxy/pcflist/512980',
}
TRANSPORT='identity-or-single-gzip-bounded-v1'
NAMESPACE=UUID('5ce1fc53-877d-5656-a8af-f148c5c997da')


class RecoveryDocument(StrictPolicy):
    fundCode:Literal['512400','512200','512980']
    mediaType:Literal['html']
    url:str

    @model_validator(mode='after')
    def closed_source(self):
        if self.url!=RECOVERY_URLS[self.fundCode]:raise ValueError('ISSUER_RECOVERY_SOURCE_INVALID')
        return self


def failure_receipt(session,row):
    from app.macro.issuer_sample_store import IssuerSampleStore
    if row is None:raise ValueError('ISSUER_RECOVERY_PARENT_REQUIRED')
    d=IssuerSampleStore(None)._run(session,row);code=d['document']['fundCode']
    expected='EXTENSION_HTTP_ERROR' if code=='512980' else 'EXTENSION_ENCODING_UNSUPPORTED'
    if code not in RECOVERY_URLS or d['phase']!='failed' or row.error_message!=expected:
        raise ValueError('ISSUER_RECOVERY_PARENT_INVALID')
    return {'runId':row.id,'plan':deepcopy(d['plan']),'detailsHash':digest(d),'errorCode':row.error_message,
        'startedAt':utc(row.started_at).isoformat(),'finishedAt':utc(row.finished_at).isoformat()}


def build_recovery_plan(factory,catalog,code,now):
    from app.macro.official_member_budget import SAMPLE_JOB_PREFIX
    at=instant(now)
    if code not in RECOVERY_URLS:raise ValueError('ISSUER_RECOVERY_TARGET_INVALID')
    registered=next((d for d in catalog.catalog_member_policy.sampleRecoveries if d.fundCode==code),None)
    if registered is None:raise ValueError('ISSUER_RECOVERY_REGISTRATION_REQUIRED')
    base=build_sample_plan(catalog,code,at)
    with factory() as session:
        rows=list(session.scalars(select(JobRun).where(JobRun.job_name==SAMPLE_JOB_PREFIX+code)))
        if len(rows)!=1:raise ValueError('ISSUER_RECOVERY_PARENT_REQUIRED')
        parent=failure_receipt(session,rows[0])
    old=parent['plan']
    if datetime.fromisoformat(parent['finishedAt'])>at or any(base[k]!=old[k] for k in ('catalogFingerprint','target','document','limits')):
        raise ValueError('ISSUER_RECOVERY_POLICY_CHANGED')
    value={**base,'document':registered.model_dump(),
        'parent':parent,'transportPolicy':TRANSPORT}
    value['fingerprint']=digest({k:v for k,v in value.items() if k!='fingerprint'})
    validate_recovery_plan(value)
    return value


def validate_recovery_plan(plan):
    try:
        if type(plan) is not dict or set(plan)!={'schemaVersion','firstBusinessDate','catalogFingerprint','document','target','limits','fingerprint','parent','transportPolicy'}:
            raise ValueError()
        doc=RecoveryDocument.model_validate(plan['document']);parent=plan['parent']
        if type(parent) is not dict or set(parent)!={'runId','plan','detailsHash','errorCode','startedAt','finishedAt'}:raise ValueError()
        validate_sample_plan(parent['plan'])
        if parent['plan']['document']['fundCode']!=doc.fundCode or parent['runId']!=sample_run_key(parent['plan']):raise ValueError()
        if type(parent['detailsHash']) is not str or re.fullmatch('[0-9a-f]{64}',parent['detailsHash']) is None:raise ValueError()
        expected='EXTENSION_HTTP_ERROR' if doc.fundCode=='512980' else 'EXTENSION_ENCODING_UNSUPPORTED'
        start=instant(datetime.fromisoformat(parent['startedAt']));finish=instant(datetime.fromisoformat(parent['finishedAt']))
        if parent['errorCode']!=expected or finish<start or finish.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()>plan['firstBusinessDate']:
            raise ValueError()
        base={k:deepcopy(v) for k,v in plan.items() if k not in ('parent','transportPolicy')}
        base['document']=deepcopy(parent['plan']['document'])
        base['fingerprint']=digest({k:v for k,v in base.items() if k!='fingerprint'})
        validate_sample_plan(base)
        if any(base[k]!=parent['plan'][k] for k in ('catalogFingerprint','target','limits')):raise ValueError()
        if plan['transportPolicy']!=TRANSPORT or digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:raise ValueError()
        return doc
    except Exception:raise ValueError('ISSUER_RECOVERY_PLAN_INVALID') from None


def recovery_run_key(plan):
    doc=validate_recovery_plan(plan)
    return str(uuid5(NAMESPACE,doc.fundCode+':'+plan['parent']['runId']))
