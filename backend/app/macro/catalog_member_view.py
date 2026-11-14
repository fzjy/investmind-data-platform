"""目录见证合并与截止可见来源状态；不采集，不用待办冒充完整成员。"""
from datetime import date,datetime,time,timezone
from zoneinfo import ZoneInfo

from sqlalchemy import inspect,or_,select

from app.db.models.ops import JobRun
from app.macro.catalog_member_store import CatalogMemberStore
from app.macro.official_member_store import JOB,OfficialMemberStore
from app.macro.official_member_budget import CATALOG_JOB_PREFIX
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import instant,FAILURE_CODES

PENDING_CODE='CATALOG_MEMBER_SOURCE_CONTRACT_PENDING'
PROOF_CODE='OFFICIAL_MEMBER_PROOF_INCOMPLETE'


def _cutoff(as_of):
    return instant(as_of) if isinstance(as_of,datetime) else datetime.combine(as_of,time.max,ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)


def load_catalog_member_witnesses(factory,as_of,catalog):
    from app.macro.enterprise_official_targets import _load_official_job_witnesses
    result={w['targetCode']:w for w in _load_official_job_witnesses(factory,as_of,catalog)}
    if catalog.catalog_member_policy is None:return list(result.values())
    registered={s.code for s in catalog.catalog_member_policy.targets if s.documents}
    for target in catalog.targets:
        if target.code not in registered:continue
        witnesses=_load_official_job_witnesses(factory,as_of,catalog,CATALOG_JOB_PREFIX+target.code,CatalogMemberStore)
        for witness in witnesses:
            previous=result.get(witness['targetCode'])
            if previous is not None:
                if previous['membershipDate']==witness['membershipDate'] and previous['members']!=witness['members']:
                    raise ValueError('OFFICIAL_MEMBER_WITNESS_CONFLICT')
                order=lambda w:(w['membershipDate'],max(d['completedAt'] for d in w['documents']))
                if order(previous)>order(witness):continue
            result[witness['targetCode']]=witness
    from app.macro.fund_member_graph_view import load_fund_graph_view
    for witness in load_fund_graph_view(factory,as_of,catalog)[0]:
        previous=result.get(witness['targetCode'])
        if previous is not None and previous!=witness:raise ValueError('OFFICIAL_MEMBER_WITNESS_CONFLICT')
        result[witness['targetCode']]=witness
    return [result[t.code] for t in catalog.targets if t.code in result]


def default_source_state(catalog,code,has_witness=False):
    if has_witness:return {'status':'verified','errorCode':None}
    scope=next((s for s in catalog.catalog_member_policy.targets if s.code==code),None) if catalog.catalog_member_policy else None
    graph=any(g.code==code for g in catalog.catalog_member_policy.memberGraphs) if catalog.catalog_member_policy else False
    configured=code in catalog.official_policy.bootstrapTargetCodes or scope is not None and bool(scope.documents) or graph
    return {'status':'not-collected','errorCode':None} if configured else {'status':'contract-pending','errorCode':PENDING_CODE}


def source_state_valid(value,has_witness):
    if type(value) is not dict or set(value)!={'status','errorCode'}:return False
    state,error=value['status'],value['errorCode']
    if state=='verified':return has_witness and error is None
    if state=='contract-pending':return not has_witness and error==PENDING_CODE
    if state=='not-collected':return not has_witness and error is None
    if state in ('request-pending','replay-pending'):return error is None
    if state=='source-failed':return type(error) is str and error in FAILURE_CODES
    if state=='proof-incomplete':return error==PROOF_CODE
    return False


def _dependent_documents(plan,target):
    docs=plan['policy']['documents']
    if target.kind=='index':return [d for d in docs if d['kind']=='csi-members' and d['indexCode']==target.code[:6]]
    fund=[d for d in docs if d['fundCode']==target.code[:6]]
    indexes={d['indexCode'] for d in fund}
    return fund+[d for d in docs if d['kind']=='csi-members' and d['indexCode'] in indexes]


def load_official_source_states(factory,as_of,catalog,witnesses=None):
    if catalog.official_policy is None:return {}
    cutoff=_cutoff(as_of)
    proofs=load_catalog_member_witnesses(factory,as_of,catalog) if witnesses is None else witnesses
    by_witness={w['targetCode']:w for w in proofs};by_target={t.code:t for t in catalog.targets}
    states={code:default_source_state(catalog,code,code in by_witness) for code in by_target}
    registered={s.code for s in catalog.catalog_member_policy.targets if s.documents} if catalog.catalog_member_policy else set()
    groups={}
    with factory() as session:
        bind=session.get_bind();schema=bind.get_execution_options().get('schema_translate_map',{}).get('ops','ops')
        if not inspect(bind).has_table('job_run',schema=schema):return states
        rows=session.scalars(select(JobRun).where(or_(JobRun.job_name==JOB,JobRun.job_name.like(CATALOG_JOB_PREFIX+'%')),
            JobRun.started_at<=cutoff).order_by(JobRun.started_at,JobRun.id)).all()
        for row in rows:
            if row.job_name!=JOB and row.job_name.removeprefix(CATALOG_JOB_PREFIX) not in registered:continue
            store=OfficialMemberStore(factory) if row.job_name==JOB else CatalogMemberStore(factory)
            d=store._run(session,row);p=d['plan']
            if p['catalogFingerprint']!=catalog.fingerprint:continue
            for code in p['policy']['bootstrapTargetCodes']:
                if code not in by_target:raise ValueError('OFFICIAL_MEMBER_TARGET_INVALID')
                needed=_dependent_documents(p,by_target[code])
                if d['document'] not in needed:continue
                group=groups.setdefault((p['fingerprint'],code),{'plan':p,'needed':needed,'rows':[]})
                group['rows'].append(row)
        ordered=sorted(groups.items(),key=lambda item:max(utc(r.started_at) for r in item[1]['rows']))
        for (_,code),group in ordered:
            phases=[];error=None
            for row in group['rows']:
                phase=row.details['phase']
                if row.finished_at is not None and utc(row.finished_at)>cutoff:
                    store=OfficialMemberStore(factory) if row.job_name==JOB else CatalogMemberStore(factory)
                    phase='received' if row.details['rawId'] is not None and store._raw(session,row)[1]<=cutoff else 'reserved'
                if phase=='received':
                    store=OfficialMemberStore(factory) if row.job_name==JOB else CatalogMemberStore(factory)
                    if store._raw(session,row)[1]>cutoff:phase='reserved'
                phases.append(phase)
                if phase=='failed':error=row.error_message
            if 'failed' in phases:state={'status':'source-failed','errorCode':error}
            elif 'reserved' in phases or len(phases)<len(group['needed']):state={'status':'request-pending','errorCode':None}
            elif 'received' in phases:state={'status':'replay-pending','errorCode':None}
            elif code in by_witness and by_witness[code]['planFingerprint']==group['plan']['fingerprint']:
                state={'status':'verified','errorCode':None}
            else:state={'status':'proof-incomplete','errorCode':PROOF_CODE}
            states[code]=state
    from app.macro.fund_member_graph_view import load_fund_graph_view
    states.update(load_fund_graph_view(factory,as_of,catalog)[1])
    return states
