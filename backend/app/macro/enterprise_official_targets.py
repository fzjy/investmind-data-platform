"""官方原件见证与实际池交集；不按全指数/完整ETF持仓解释子样本。"""
from copy import deepcopy
from datetime import date,datetime,time,timezone
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import inspect,select

from app.db.models.ops import JobCursor,JobRun
from app.macro.enterprise_industries import parent_periods,subset_enterprise_inputs
from app.macro.extensions.ingest import utc
from app.macro.extensions.store import instant
from app.macro.official_member_store import JOB,OfficialMemberStore
from app.macro.catalog_member_view import load_official_source_states,source_state_valid,default_source_state
from app.macro.nffund_contracts import CONTRACTS as NFFUND_CONTRACTS,METADATA_URL,pcf_template
from app.macro import fsfund_contracts as fsfund
from app.macro import consumer_xml_contracts as consumer
from app.macro import jsfund_contracts as jsfund
from app.macro import etf_definition_contracts as definition
from app.macro import media_contracts as media

BASIS='official-current-members-pool-intersection-restated-history-not-strict-pit'
WITNESS_KEYS={'targetCode','kind','indexCode','membershipDate','members','memberCount','planFingerprint',
    'catalogFingerprint','membershipBasis','trackingIndexName','documents'}
DOC_KEYS={'kind','url','rawSha256','runId','retrievedAt','completedAt'}


def valid_witness(value,target_code,kind,as_of):
    try:
        if (type(value) is not dict or set(value)!=WITNESS_KEYS or value['targetCode']!=target_code or value['kind']!=kind
            or kind not in ('index','etf') or re.fullmatch(r'[0-9]{6}\.(SH|SZ)',target_code) is None
            or re.fullmatch('[0-9]{6}',value['indexCode']) is None
            or type(value['memberCount']) is not int or not 1<=value['memberCount']<=10000):return False
        members=value['members'];day=date.fromisoformat(value['membershipDate'])
        if (day.isoformat()!=value['membershipDate'] or day>as_of or type(members) is not list
            or any(type(s) is not str or re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)',s) is None for s in members)
            or members!=sorted(set(members)) or len(members)!=value['memberCount']
            or len({s[:6] for s in members})!=len(members)
            or any(type(value[k]) is not str or re.fullmatch('[0-9a-f]{64}',value[k]) is None for k in ('planFingerprint','catalogFingerprint'))):return False
        if kind=='index':
            if (value['indexCode']!=target_code[:6] or value['trackingIndexName'] is not None
                or value['membershipBasis']!='official-csi-complete-file'):return False
        elif (type(value['trackingIndexName']) is not str or not value['trackingIndexName'].endswith('指数')
            or value['membershipBasis']!='official-fund-statement-and-pcf-csi-same-date'):return False
        nf=NFFUND_CONTRACTS.get(target_code[:6]) if kind=='etf' else None
        bank=kind=='etf' and target_code==fsfund.FUND_CODE+'.SH'
        consumption=kind=='etf' and target_code==consumer.FUND_CODE+'.SZ'
        chip=kind=='etf' and target_code==jsfund.FUND_CODE+'.SH'
        def_profile=definition.PROFILES.get(target_code[:6]) if kind=='etf' else None
        ev=def_profile is not None and target_code=='515030.SH'
        media_fund=kind=='etf' and target_code==media.FUND_CODE+'.SH'
        if media_fund and (value['indexCode']!=media.INDEX_CODE or value['trackingIndexName']!=media.INDEX_NAME):return False
        if def_profile is not None and (not target_code.endswith('.SH') or value['indexCode']!=def_profile[0] or value['trackingIndexName']!=def_profile[1]):return False
        if chip and (value['indexCode']!=jsfund.INDEX_CODE or value['trackingIndexName']!=jsfund.INDEX_NAME):return False
        if consumption and (value['indexCode']!=consumer.INDEX_CODE or value['trackingIndexName']!=consumer.INDEX_NAME):return False
        if nf is not None and (value['indexCode']!=nf[0] or value['trackingIndexName']!=nf[1]):return False
        if bank and (value['indexCode']!=fsfund.INDEX_CODE or value['trackingIndexName']!=fsfund.INDEX_NAME):return False
        docs=value['documents'];kinds=['csi-members'] if kind=='index' else ['csi-members','fund-mapping','fund-pcf']
        if type(docs) is not list or len(docs)!=len(kinds) or [d['kind'] for d in docs]!=kinds:return False
        if len({d['runId'] for d in docs})!=len(docs):return False
        for d in docs:
            expected_keys=DOC_KEYS|{'requestParameters'} if bank and d['kind']=='fund-pcf' or (chip or ev) and d['kind'] in ('fund-mapping','fund-pcf') else DOC_KEYS
            if media_fund and d['kind']=='fund-pcf':expected_keys=DOC_KEYS|{'responseIdentity'}
            if (type(d) is not dict or set(d)!=expected_keys or re.fullmatch('[0-9a-f]{64}',d['rawSha256']) is None
                or str(UUID(d['runId']))!=d['runId']):return False
            retrieved=instant(datetime.fromisoformat(d['retrievedAt']));completed=instant(datetime.fromisoformat(d['completedAt']))
            # 公开企业合同精度为上海分析日；加载器另按实际精确截止时刻把关。
            cutoff=datetime.combine(as_of,time.max,ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
            if (retrieved.isoformat()!=d['retrievedAt'] or completed.isoformat()!=d['completedAt'] or completed<retrieved
                or retrieved>cutoff or completed>cutoff):return False
            if d['kind']=='csi-members':
                if d['url']!=f"https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{value['indexCode']}cons.xls":return False
            elif d['kind']=='fund-mapping':
                if media_fund:
                    if d['url']!=media.PDF_URL or retrieved.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()<media.STATEMENT_DATE:return False
                    continue
                if def_profile is not None:
                    source_day=definition.statement_day(target_code[:6],d['url'],d.get('requestParameters'))
                    if source_day>retrieved.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():return False
                    continue
                if chip:
                    if d['url']!=jsfund.URL or d['requestParameters']!=jsfund.public_parameters('fund-mapping'):return False
                    continue
                if consumption:
                    source_day=consumer.statement_day(d['url'])
                    if source_day>retrieved.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():return False
                    continue
                expected=fsfund.METADATA_URL if bank else METADATA_URL if nf is not None else f'https://e.gtfund.com/etrade/Jijin/view/id/{target_code[:6]}#etf-list'
                if d['url']!=expected:return False
            elif media_fund:
                if d['url']!=media.PCF_URL or d['responseIdentity']!={'fundCode':media.FUND_CODE,'tradingDate':value['membershipDate']}:return False
            elif def_profile is not None:
                if d['url']!=definition.source_url(target_code[:6],value['membershipDate']):return False
                if ev and d['requestParameters']!=definition.ev_parameters(value['membershipDate']):return False
            elif chip:
                if d['url']!=jsfund.URL or d['requestParameters']!=jsfund.public_parameters('fund-pcf',value['membershipDate']):return False
            elif consumption:
                if d['url']!=consumer.pcf_url(value['membershipDate']):return False
            elif bank:
                if d['url']!=fsfund.PCF_URL or d['requestParameters']!={'fundCode':fsfund.FUND_CODE,'startDate':day.strftime('%Y%m%d')}:return False
            elif nf is not None:
                if d['url']!=pcf_template(target_code[:6],nf[2]).format(yyyymmdd=day.strftime('%Y%m%d')):return False
            elif re.fullmatch(r'https://m\.gtfund\.com/cochin/etf/download/[0-9]{6}/'+day.strftime('%Y%m%d'),d['url']) is None:return False
        return True
    except (ValueError,TypeError,KeyError,AttributeError):return False


def _load_official_job_witnesses(factory,as_of,catalog,job_name=JOB,store_type=OfficialMemberStore):
    if catalog.official_policy is None:return []
    cutoff=instant(as_of) if isinstance(as_of,datetime) else datetime.combine(as_of,time.max,ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
    targets={t.code:t for t in catalog.targets};result={}
    with factory() as session:
        bind=session.get_bind();schema='ops'
        if not inspect(bind).has_table('job_run',schema=schema):return []
        store=store_type(factory)
        roots=session.scalars(select(JobCursor).where(JobCursor.job_name==job_name).order_by(JobCursor.cursor_key)).all()
        for root in roots:
            if type(root.cursor_value) is not dict or set(root.cursor_value)!={'plan'}:raise ValueError('OFFICIAL_MEMBER_ROOT_INVALID')
            plan=root.cursor_value['plan'];store._root(session,plan)
            if plan['catalogFingerprint']!=catalog.fingerprint or plan['businessDate']>cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():continue
            rows=session.scalars(select(JobRun).where(JobRun.job_name==job_name,JobRun.started_at<=cutoff)).all()
            ready=[]
            for row in rows:
                if row.details.get('plan',{}).get('businessDate')!=plan['businessDate']:continue
                d=store._run(session,row)
                if d['phase']=='completed' and utc(row.finished_at)<=cutoff:
                    raw,retrieved=store._raw(session,row)
                    ready.append((d['document'],d['normalized'],{'kind':d['document']['kind'],'url':d['document']['url'],
                        'rawSha256':d['normalized']['rawSha256'],'runId':row.id,
                        'retrievedAt':retrieved.isoformat(),'completedAt':utc(row.finished_at).isoformat()}))
            for code in plan['policy']['bootstrapTargetCodes']:
                target=targets[code];chosen=[];tracking=None
                if target.kind=='index':
                    chosen=[r for r in ready if r[0]['kind']=='csi-members' and r[0]['indexCode']==code[:6]]
                    if len(chosen)!=1:continue
                else:
                    fund=[r for r in ready if r[0]['kind']=='fund-mapping' and r[0]['fundCode']==code[:6]]
                    pcf=[r for r in ready if r[0]['kind']=='fund-pcf' and r[0]['fundCode']==code[:6]]
                    if len(fund)!=1 or len(pcf)!=1 or fund[0][0]['indexCode']!=pcf[0][0]['indexCode']:continue
                    csi=[r for r in ready if r[0]['kind']=='csi-members' and r[0]['indexCode']==fund[0][0]['indexCode']]
                    if (len(csi)!=1 or fund[0][1]['trackingIndexName']!=target.source_index_name_hint
                        or csi[0][1]['asOfDate']!=pcf[0][1]['asOfDate']
                        or sorted(s[:6] for s in csi[0][1]['members'])!=pcf[0][1]['memberCodes']):continue
                    chosen=[csi[0],fund[0],pcf[0]];tracking=fund[0][1]['trackingIndexName']
                csi=chosen[0]
                proof={'targetCode':code,'kind':target.kind,'indexCode':csi[0]['indexCode'],
                    'membershipDate':csi[1]['asOfDate'],'members':csi[1]['members'],'memberCount':csi[1]['memberCount'],
                    'planFingerprint':plan['fingerprint'],'catalogFingerprint':plan['catalogFingerprint'],
                    'membershipBasis':'official-csi-complete-file' if target.kind=='index' else 'official-fund-statement-and-pcf-csi-same-date',
                    'trackingIndexName':tracking,'documents':[r[2] for r in chosen]}
                if not valid_witness(proof,code,target.kind,cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date()):raise ValueError('OFFICIAL_WITNESS_INVALID')
                result[code]=proof
    return list(result.values())


def load_official_witnesses(factory,as_of,catalog):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    return load_catalog_member_witnesses(factory,as_of,catalog)


def build_official_target_comparison(inputs,as_of,policy,parent,catalog,witnesses,source_states=None):
    from app.macro.enterprise_contract import public_enterprise_analysis
    if catalog.official_policy is None:return None
    by_code={t.code:t for t in catalog.targets};by_witness={w['targetCode']:w for w in witnesses}
    if len(by_witness)!=len(witnesses):raise ValueError('OFFICIAL_WITNESS_INVALID')
    expanded=catalog.catalog_member_policy is not None
    codes=[t.code for t in catalog.targets] if expanded else catalog.official_policy.bootstrapTargetCodes
    if source_states is not None and not set(source_states)<=set(codes):raise ValueError('OFFICIAL_TARGET_SOURCE_STATE_INVALID')
    result={'schemaVersion':'1.1' if expanded else '1.0','basis':BASIS,'asOfDate':parent['asOfDate'],'sourceHash':parent['inputHash'],'targets':[]}
    for code in codes:
        target=by_code[code];w=by_witness.get(code)
        row={'code':code,'kind':target.kind,'label':target.label,'status':'evidence-missing','witness':None,'poolMembers':[],'analysis':None}
        if expanded:row['sourceState']=deepcopy((source_states or {}).get(code,default_source_state(catalog,code,w is not None)))
        if w is not None:
            if not valid_witness(w,code,target.kind,as_of) or w['catalogFingerprint']!=catalog.fingerprint:raise ValueError('OFFICIAL_WITNESS_INVALID')
            members=sorted(set(w['members'])&set(inputs.symbols));row.update(witness=deepcopy(w),poolMembers=members,status='available' if members else 'no-intersection')
            if members:
                child=subset_enterprise_inputs(inputs,members,parent_periods(parent))
                row['analysis']=public_enterprise_analysis(child,as_of,policy,rea_enabled=True)
        result['targets'].append(row)
    if not valid_official_target_comparison(result,parent):raise ValueError('OFFICIAL_TARGET_CONTRACT_INVALID')
    return result


def valid_official_target_comparison(value,parent):
    from app.macro.enterprise_contract import valid_enterprise_analysis
    from app.macro.enterprise_rea import same_rea_mode
    try:
        if (type(value) is not dict or set(value)!={'schemaVersion','basis','asOfDate','sourceHash','targets'}
            or value['schemaVersion'] not in ('1.0','1.1') or value['basis']!=BASIS or value['asOfDate']!=parent['asOfDate']
            or value['sourceHash']!=parent['inputHash'] or type(value['targets']) is not list
            or not 1<=len(value['targets'])<=(2 if value['schemaVersion']=='1.0' else 24)):return False
        codes=[]
        for t in value['targets']:
            keys={'code','kind','label','status','witness','poolMembers','analysis'}|({'sourceState'} if value['schemaVersion']=='1.1' else set())
            if (type(t) is not dict or set(t)!=keys
                or re.fullmatch(r'[0-9]{6}\.(SH|SZ)',t['code']) is None or t['kind'] not in ('index','etf')
                or type(t['label']) is not str or not 1<=len(t['label'])<=160):return False
            codes.append(t['code']);w=t['witness'];child=t['analysis']
            if value['schemaVersion']=='1.1' and not source_state_valid(t['sourceState'],w is not None):return False
            if w is None:
                if t['status']!='evidence-missing' or t['poolMembers']!=[] or child is not None:return False
                continue
            if not valid_witness(w,t['code'],t['kind'],date.fromisoformat(parent['asOfDate'])):return False
            members=sorted(set(w['members'])&set(parent['symbols']))
            if t['poolMembers']!=members:return False
            if not members:
                if t['status']!='no-intersection' or child is not None:return False
                continue
            if (t['status']!='available' or type(child) is not dict or 'officialTargets' in child or 'industryComparison' in child
                or child['symbols']!=members or child['asOfDate']!=parent['asOfDate'] or child['poolVersion']!=parent['poolVersion']
                or child['policy']!=parent['policy'] or child['algorithmVersion']!=parent['algorithmVersion']
                or not same_rea_mode(child,parent)
                or child.get('industryRoot')!=parent.get('industryRoot') or not valid_enterprise_analysis(child)):
                return False
            if any([q['period'] for q in m['quality']]!=parent_periods(parent) for m in child['cards']+child['rea']['series']):return False
        return len(codes)==len(set(codes))
    except (ValueError,TypeError,KeyError,AttributeError):return False
