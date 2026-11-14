"""两叶固定来源图；声明仅引用原始收据，不复制Raw或伪造零请求槽。"""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select

from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import instant
from app.macro.official_member_budget import family_lock,DISCOVERY_JOB_PREFIX
from app.macro.official_member_store import OfficialMemberStore
from app.macro.fund_contract_store import FundContractStore
from app.macro.fund_member_graph import (PREFIX,build_fund_graph_leaf,fund_graph_run_key,
    validate_fund_graph_intent,validate_fund_graph_leaf)
from app.macro.official_members import parse_csi_members,parse_fund_pcf


def _receipt(store,session,row):
    d=store._run(session,row)
    if d['phase']!='completed':raise ValueError('FUND_MEMBER_GRAPH_SOURCE_NOT_COMPLETE')
    _,retrieved=store._raw(session,row)
    return {'runId':row.id,'plan':deepcopy(d['plan']),'rawId':d['rawId'],'rawSha256':d['normalized']['rawSha256'],
        'retrievedAt':retrieved.isoformat(),'completedAt':utc(row.finished_at).isoformat(),'normalized':deepcopy(d['normalized'])}


def load_source_contract_state(factory,catalog,code,as_of):
    at=instant(as_of)
    try:
        scope=next(s for s in catalog.catalog_member_policy.memberGraphs if s.code==code)
        doc=next(d for d in catalog.catalog_member_policy.discoveryDocuments if d.id==scope.discoveryDocumentId)
    except (AttributeError,StopIteration):raise ValueError('FUND_MEMBER_GRAPH_REGISTRATION_REQUIRED') from None
    with factory() as session:
        rows=session.scalars(select(JobRun).where(JobRun.job_name==DISCOVERY_JOB_PREFIX+doc.fundCode)).all()
        result={'phase':'new','proof':None,'errorCode':None,'runId':None}
        if not rows:return result
        if len(rows)!=1:raise ValueError('FUND_MEMBER_GRAPH_SOURCE_INVALID')
        row=rows[0]
        if utc(row.started_at)>at:return result
        store=FundContractStore(factory);d=store._run(session,row)
        if d['document']!=doc.model_dump() or d['plan']['catalogFingerprint']!=catalog.fingerprint:
            raise ValueError('FUND_MEMBER_GRAPH_SOURCE_CHANGED')
        phase=d['phase'];retrieved=store._raw(session,row)[1] if d['rawId'] is not None else None
        if row.finished_at is not None and utc(row.finished_at)>at:phase='received' if retrieved is not None and retrieved<=at else 'reserved'
        if phase=='received' and retrieved>at:phase='reserved'
        result.update(phase=phase,runId=row.id,errorCode=row.error_message if phase=='failed' else None)
        if phase=='completed':result['proof']=_receipt(store,session,row)
        return result


def resolve_source_contract(factory,catalog,code,as_of):
    return load_source_contract_state(factory,catalog,code,as_of)['proof']


class FundMemberGraphStore(OfficialMemberStore):
    def intent_job(self,intent):return PREFIX+intent['target']['code']
    def job_name(self,plan):return self.intent_job(plan['intent'])
    def business_key(self,plan,document):return 'fund-member-graph:'+plan['intent']['target']['code']+':'+document['kind']
    def documents(self,plan):return [plan['document']]
    def run_key(self,plan,document):return fund_graph_run_key(plan['intent'],document['kind'])
    def max_response_bytes(self,plan):return plan['intent']['limits']['maxResponseBytes']
    def dataset(self,plan):return 'enterprise-fund-member-graph'
    def api_name(self,plan,document):return 'official-fund-members/'+document['kind']

    def _source_contract(self,session,intent):
        validate_fund_graph_intent(intent);expected=intent['sourceContract']
        source=FundContractStore(self.factory);row=session.get(JobRun,expected['runId'])
        if row is None or _receipt(source,session,row)!=expected:raise ValueError('FUND_MEMBER_GRAPH_SOURCE_INVALID')

    def _intent_root(self,session,intent):
        self._source_contract(session,intent)
        root=session.scalar(select(JobCursor).where(JobCursor.job_name==self.intent_job(intent),JobCursor.cursor_key==intent['target']['code']))
        if (root is None or root.cursor_value!={'intent':intent}
            or utc(root.updated_at)<datetime.fromisoformat(intent['createdAt'])
            or utc(root.updated_at).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()!=intent['firstBusinessDate']):
            raise ValueError('FUND_MEMBER_GRAPH_ROOT_INVALID')
        return root

    def _root(self,session,plan):
        validate_fund_graph_leaf(plan)
        return self._intent_root(session,plan['intent'])

    def _run(self,session,row):
        d=super()._run(session,row);p=d['plan'];root=self._intent_root(session,p['intent']);at=utc(row.started_at)
        if at!=datetime.fromisoformat(p['reservedAt']) or at<utc(root.updated_at):raise ValueError('FUND_MEMBER_GRAPH_RUN_TIME_INVALID')
        if d['document']['kind']=='csi-members':
            if at!=utc(root.updated_at):raise ValueError('FUND_MEMBER_GRAPH_ROOT_TIME_INVALID')
        else:
            parent=session.get(JobRun,fund_graph_run_key(p['intent'],'csi-members'))
            if parent is None or _receipt(self,session,parent)!=p['csiReceipt']:raise ValueError('FUND_MEMBER_GRAPH_PARENT_INVALID')
        return d

    def normalize(self,plan,document,body):
        intent=plan['intent'];limits=intent['limits']
        if document['kind']=='csi-members':
            value=parse_csi_members(body,intent['indexCode'],max_members=limits['maxSnapshotMembers'])
            if value['asOfDate']>datetime.fromisoformat(plan['reservedAt']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
                raise ValueError('FUND_MEMBER_GRAPH_SOURCE_FUTURE')
            return value
        from app.macro.fsfund_contracts import FUND_CODE,parse_fsfund_pcf
        from app.macro.consumer_xml_contracts import FUND_CODE as CONSUMER_CODE,parse_consumer_xml_pcf
        from app.macro.jsfund_contracts import FUND_CODE as CHIP_CODE,parse_jsfund_pcf
        from app.macro.etf_definition_contracts import PROFILES as DEFINITION_PROFILES,parse_definition_pcf
        from app.macro.media_contracts import FUND_CODE as MEDIA_CODE,parse_media_pcf
        if document['fundCode'] in (FUND_CODE,CONSUMER_CODE,CHIP_CODE,MEDIA_CODE,*DEFINITION_PROFILES):
            parser=(parse_media_pcf if document['fundCode']==MEDIA_CODE else parse_definition_pcf if document['fundCode'] in DEFINITION_PROFILES else parse_jsfund_pcf if document['fundCode']==CHIP_CODE
                else parse_consumer_xml_pcf if document['fundCode']==CONSUMER_CODE else parse_fsfund_pcf)
            value=parser(body,document['fundCode'],document['tradingDate'],max_members=limits['maxSnapshotMembers'])
            if value['qualifiedMemberCodes']!=plan['csiReceipt']['normalized']['members']:raise ValueError('FUND_MEMBER_GRAPH_MEMBER_SET_MISMATCH')
        else:value=parse_fund_pcf(body,document['fundCode'],document['tradingDate'],max_members=limits['maxSnapshotMembers'])
        expected=sorted(s[:6] for s in plan['csiReceipt']['normalized']['members'])
        if value['memberCodes']!=expected:raise ValueError('FUND_MEMBER_GRAPH_MEMBER_SET_MISMATCH')
        return value

    def source_receipt(self,session,row):return _receipt(self,session,row)

    def inspect(self,intent,now):
        at=instant(now);validate_fund_graph_intent(intent)
        if datetime.fromisoformat(intent['createdAt'])>at:raise ValueError('FUND_MEMBER_GRAPH_CLOCK_REGRESSION')
        with self.factory() as session:
            roots=session.scalars(select(JobCursor).where(JobCursor.job_name==self.intent_job(intent))).all()
            rows=session.scalars(select(JobRun).where(JobRun.job_name==self.intent_job(intent))).all()
            if len(roots)>1 or len(rows)>2 or bool(roots)!=bool(rows):raise ValueError('FUND_MEMBER_GRAPH_ROOT_INVALID')
            chosen=intent
            if roots:
                root=roots[0]
                if type(root.cursor_value) is not dict or set(root.cursor_value)!={'intent'}:raise ValueError('FUND_MEMBER_GRAPH_ROOT_INVALID')
                chosen=root.cursor_value['intent'];self._intent_root(session,chosen)
                ignored={'firstBusinessDate','createdAt','fingerprint'}
                if ({k:v for k,v in chosen.items() if k not in ignored}!={k:v for k,v in intent.items() if k not in ignored}
                    or utc(root.updated_at)>at):raise ValueError('FUND_MEMBER_GRAPH_POLICY_CHANGED')
            else:self._source_contract(session,chosen)
            by_kind={}
            for row in rows:
                d=self._run(session,row);kind=d['document']['kind']
                if (kind in by_kind or d['plan']['intent']!=chosen or utc(row.started_at)>at
                    or row.finished_at is not None and utc(row.finished_at)>at
                    or d['rawId'] is not None and self._raw(session,row)[1]>at):raise ValueError('FUND_MEMBER_GRAPH_CLOCK_REGRESSION')
                by_kind[kind]=row
            sources=[];csi=by_kind.get('csi-members');csi_proof=None
            if csi is not None and csi.details['phase']=='completed':csi_proof=_receipt(self,session,csi)
            for kind in ('csi-members','fund-pcf'):
                row=by_kind.get(kind)
                sources.append({'kind':kind,'runId':row.id if row else None,'phase':row.details['phase'] if row else
                    'waiting-for-csi' if kind=='fund-pcf' and csi_proof is None else 'new','errorCode':row.error_message if row else None})
            phases=[s['phase'] for s in sources]
            status=('blocked' if any(p in ('reserved','failed') for p in phases) else 'replay_pending' if 'received' in phases else
                'completed' if phases==['completed','completed'] else 'partial' if csi_proof is not None else 'new')
            return {'status':status,'intent':chosen,'sources':sources,'csiReceipt':csi_proof,'witnessReady':status=='completed',
                'dailyBudget':self.budget(session,at,chosen['limits']['maxHttpPerDay'])}

    def reserve(self,intent,kind,now):
        at=instant(now);validate_fund_graph_intent(intent)
        with self.factory.begin() as session:
            family_lock(session);state=self.inspect(intent,at);chosen=state['intent'];self._source_contract(session,chosen)
            source=next((s for s in state['sources'] if s['kind']==kind),None)
            if state['status'] in ('blocked','replay_pending','completed') or source is None or source['phase']!='new':
                raise ValueError('FUND_MEMBER_GRAPH_RESERVATION_BLOCKED')
            if self.budget(session,at,chosen['limits']['maxHttpPerDay'])['remaining']==0:
                raise ValueError('OFFICIAL_MEMBER_DAILY_BUDGET_EXHAUSTED')
            parent=None
            if kind=='fund-pcf':
                row=session.get(JobRun,fund_graph_run_key(chosen,'csi-members'))
                if row is None:raise ValueError('FUND_MEMBER_GRAPH_PARENT_INVALID')
                parent=_receipt(self,session,row)
            child=build_fund_graph_leaf(chosen,kind,at,parent);key=fund_graph_run_key(chosen,kind)
            root=session.scalar(select(JobCursor).where(JobCursor.job_name==self.intent_job(chosen),JobCursor.cursor_key==chosen['target']['code']))
            if root is None:
                if kind!='csi-members' or chosen['firstBusinessDate']!=at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
                    raise ValueError('FUND_MEMBER_GRAPH_UNUSED_INTENT_EXPIRED')
                session.add(JobCursor(job_name=self.intent_job(chosen),cursor_key=chosen['target']['code'],cursor_value={'intent':chosen},updated_at=at));session.flush()
            else:self._intent_root(session,chosen)
            session.add(JobRun(id=key,job_name=self.intent_job(chosen),business_key=self.business_key(child,child['document']),
                status='running',started_at=at,request_count=1,row_count=0,
                details={'schemaVersion':1,'plan':child,'document':child['document'],'phase':'reserved','rawId':None,'normalized':None}))
            session.add(IngestionBatch(id=key,source='official',dataset=self.dataset(child),mode='live',status='running',
                started_at=at,request_count=1,row_count=0,rejected_count=0))
        return key
