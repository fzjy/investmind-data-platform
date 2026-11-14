"""按精确截止读取三原件证明；只读，不把声明或单CSI当成完整成员。"""
from datetime import datetime,time,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import inspect,select

from app.db.models.ops import JobCursor,JobRun
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import instant
from app.macro.extensions.plan import digest
from app.macro.fund_member_graph import PREFIX,fund_graph_run_key
from app.macro.fund_member_graph_store import FundMemberGraphStore,load_source_contract_state


def _cutoff(as_of):
    return instant(as_of) if isinstance(as_of,datetime) else datetime.combine(as_of,time.max,ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)


def load_fund_graph_view(factory,as_of,catalog):
    policy=catalog.catalog_member_policy
    if policy is None or not policy.memberGraphs:return [],{}
    cutoff=_cutoff(as_of);witnesses=[];states={g.code:{'status':'not-collected','errorCode':None} for g in policy.memberGraphs};store=FundMemberGraphStore(factory)
    with factory() as session:
        bind=session.get_bind();schema=bind.get_execution_options().get('schema_translate_map',{}).get('ops','ops')
        if any(not inspect(bind).has_table(name,schema=schema) for name in ('job_cursor','job_run')):return [],states
        for scope in policy.memberGraphs:
            code=scope.code;states[code]={'status':'not-collected','errorCode':None}
            parent=load_source_contract_state(factory,catalog,code,cutoff)
            if parent['phase']!='completed':
                states[code]=({'status':'source-failed','errorCode':parent['errorCode']} if parent['phase']=='failed' else
                    {'status':'request-pending','errorCode':None} if parent['phase']=='reserved' else
                    {'status':'replay-pending','errorCode':None} if parent['phase']=='received' else
                    {'status':'contract-pending','errorCode':'CATALOG_MEMBER_SOURCE_CONTRACT_PENDING'})
                continue
            roots=session.scalars(select(JobCursor).where(JobCursor.job_name==PREFIX+code)).all()
            if not roots:continue
            if len(roots)!=1:raise ValueError('FUND_MEMBER_GRAPH_ROOT_INVALID')
            root=roots[0]
            if utc(root.updated_at)>cutoff:continue
            if type(root.cursor_value) is not dict or set(root.cursor_value)!={'intent'}:raise ValueError('FUND_MEMBER_GRAPH_ROOT_INVALID')
            intent=root.cursor_value['intent'];store._intent_root(session,intent)
            if intent['catalogFingerprint']!=catalog.fingerprint:continue
            doc=next(d for d in policy.discoveryDocuments if d.id==scope.discoveryDocumentId)
            if intent['sourceContract']['plan']['document']!=doc.model_dump():raise ValueError('FUND_MEMBER_GRAPH_SOURCE_CHANGED')
            target=next(t for t in catalog.targets if t.code==code)
            if intent['target']!={'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint}:
                raise ValueError('FUND_MEMBER_GRAPH_TARGET_CHANGED')
            visible={};phases=[];error=None
            rows=session.scalars(select(JobRun).where(JobRun.job_name==PREFIX+code,JobRun.started_at<=cutoff)).all()
            for row in rows:
                d=store._run(session,row);kind=d['document']['kind'];phase=d['phase']
                if d['plan']['intent']!=intent:raise ValueError('FUND_MEMBER_GRAPH_ROOT_INVALID')
                received=store._raw(session,row)[1] if d['rawId'] is not None else None
                if row.finished_at is not None and utc(row.finished_at)>cutoff:phase='received' if received is not None and received<=cutoff else 'reserved'
                if phase=='received' and received>cutoff:phase='reserved'
                phases.append(phase)
                if phase=='failed':error=row.error_message
                if phase=='completed':
                    if kind in visible:raise ValueError('FUND_MEMBER_GRAPH_DUPLICATE_SOURCE')
                    visible[kind]=store.source_receipt(session,row)
            if 'failed' in phases:states[code]={'status':'source-failed','errorCode':error};continue
            if 'reserved' in phases:states[code]={'status':'request-pending','errorCode':None};continue
            if 'received' in phases:states[code]={'status':'replay-pending','errorCode':None};continue
            if set(visible)!={'csi-members','fund-pcf'}:
                states[code]={'status':'request-pending','errorCode':None};continue
            csi=visible['csi-members'];pcf=visible['fund-pcf'];contract=intent['sourceContract']
            if any(datetime.fromisoformat(r['completedAt'])>cutoff for r in (csi,contract,pcf)):continue
            def document(r,kind,url):
                value={'kind':kind,'url':url,'rawSha256':r['rawSha256'],'runId':r['runId'],
                    'retrievedAt':r['retrievedAt'],'completedAt':r['completedAt']}
                from app.macro.fsfund_contracts import FUND_CODE
                if kind=='fund-pcf' and code==FUND_CODE+'.SH':
                    value['requestParameters']={'fundCode':FUND_CODE,'startDate':r['plan']['document']['tradingDate'].replace('-','')}
                from app.macro.jsfund_contracts import FUND_CODE as CHIP_CODE,public_parameters
                if code==CHIP_CODE+'.SH' and kind in ('fund-mapping','fund-pcf'):
                    value['requestParameters']=public_parameters(kind,r['plan']['document']['tradingDate'])
                if code=='515030.SH' and kind in ('fund-mapping','fund-pcf'):
                    from app.macro.etf_definition_contracts import ev_parameters
                    value['requestParameters']=ev_parameters(r['plan']['document']['tradingDate'])
                if code=='512980.SH' and kind=='fund-pcf':
                    value['responseIdentity']={'fundCode':r['normalized']['fundCode'],'tradingDate':r['normalized']['asOfDate']}
                return value
            proof={'targetCode':code,'kind':'etf','indexCode':intent['indexCode'],'membershipDate':csi['normalized']['asOfDate'],
                'members':csi['normalized']['members'],'memberCount':csi['normalized']['memberCount'],
                'catalogFingerprint':intent['catalogFingerprint'],'planFingerprint':digest({'intent':intent['fingerprint'],
                    'sources':[{'runId':r['runId'],'planFingerprint':r['plan']['fingerprint'],'rawSha256':r['rawSha256']} for r in (csi,contract,pcf)]}),
                'membershipBasis':'official-fund-statement-and-pcf-csi-same-date','trackingIndexName':contract['normalized']['trackingIndexName'],
                'documents':[document(csi,'csi-members',csi['plan']['document']['url']),
                    document(contract,'fund-mapping',contract['plan']['document']['url']),document(pcf,'fund-pcf',pcf['plan']['document']['url'])]}
            from app.macro.enterprise_official_targets import valid_witness
            if not valid_witness(proof,code,'etf',cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date()):raise ValueError('FUND_MEMBER_GRAPH_WITNESS_INVALID')
            witnesses.append(proof);states[code]={'status':'verified','errorCode':None}
    return witnesses,states
