"""声明收据→CSI实际成员日→同日PCF的纯计划；本层不代替数据库原件验证。"""
from copy import deepcopy
from datetime import date,datetime
import re
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from pydantic import Field
from app.macro.official_member_policy import StrictPolicy,OfficialSource
from app.macro.fund_contract_discovery import BOUNDS,validate_discovery_plan,run_key as discovery_run_key
from app.macro.extensions.plan import digest
from app.macro.extensions.store import instant
from app.macro.official_member_budget import GRAPH_JOB_PREFIX
from app.macro.nffund_contracts import CONTRACTS as NFFUND_CONTRACTS,valid_resource,pcf_template
from app.macro.fsfund_contracts import FUND_CODE as BANK_FUND_CODE,PCF_URL as BANK_PCF_URL
from app.macro.consumer_xml_contracts import FUND_CODE as CONSUMER_CODE,PCF_TEMPLATE as CONSUMER_TEMPLATE
from app.macro.jsfund_contracts import FUND_CODE as CHIP_CODE,URL as CHIP_URL,canonical_index_name
from app.macro.etf_definition_contracts import EV_URL,MEDICINE_TEMPLATE
from app.macro.media_contracts import FUND_CODE as MEDIA_CODE,PCF_URL as MEDIA_PCF_URL

PREFIX=GRAPH_JOB_PREFIX
NAMESPACE=UUID('7d13ba24-75e3-5788-9e79-cbca8b198f52')
GRAPH_BOUNDS={**BOUNDS,'maxSnapshotMembers':10000}
RECEIPT_KEYS={'runId','plan','rawId','rawSha256','retrievedAt','completedAt','normalized'}


class FundMemberGraphScope(StrictPolicy):
    code:str
    discoveryDocumentId:str=Field(min_length=1,max_length=40)


def _day(value):
    if type(value) is not str or date.fromisoformat(value).isoformat()!=value:raise ValueError()
    return value


def _stamp(value):
    if type(value) is not str:raise ValueError()
    result=instant(datetime.fromisoformat(value))
    if result.isoformat()!=value:raise ValueError()
    return result


def _sha(value):return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def _receipt_shape(value):
    if (type(value) is not dict or set(value)!=RECEIPT_KEYS or type(value['rawId']) is not int or value['rawId']<=0
        or not _sha(value['rawSha256']) or type(value['normalized']) is not dict):raise ValueError()
    if _stamp(value['completedAt'])<_stamp(value['retrievedAt']):raise ValueError()
    if value['normalized'].get('rawSha256')!=value['rawSha256']:raise ValueError()


def _contract(proof,target,catalog_fingerprint,as_of):
    _receipt_shape(proof);doc=validate_discovery_plan(proof['plan']);v=proof['normalized'];p=proof['plan']
    if (proof['runId']!=discovery_run_key(p) or p['catalogFingerprint']!=catalog_fingerprint or p['target']!=target
        or type(v) is not dict or set(v)!={'schemaVersion','fundCode','fundName','trackingIndexName','rawSha256','downloadResourceKey'}
        or v['schemaVersion']!='1.0' or v['fundCode']!=doc.fundCode or doc.fundCode!=target['code'][:6]
        or type(v['fundName']) is not str or not 1<=len(v['fundName'])<=160
        or v['trackingIndexName']!=(canonical_index_name(doc.fundCode,target['sourceIndexNameHint']) if doc.fundCode==CHIP_CODE else target['sourceIndexNameHint'])
        or not valid_resource(doc.fundCode,v['downloadResourceKey'])
        or _stamp(proof['completedAt'])>as_of
        or _stamp(proof['retrievedAt']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()<p['firstBusinessDate']):raise ValueError()
    return doc


def _sources(index,fund,key):
    template=(MEDIA_PCF_URL if fund==MEDIA_CODE else EV_URL if fund=='515030' else MEDICINE_TEMPLATE if fund=='512010' else CHIP_URL if fund==CHIP_CODE else CONSUMER_TEMPLATE if fund==CONSUMER_CODE else pcf_template(fund,key) if fund in NFFUND_CONTRACTS else
        BANK_PCF_URL if fund==BANK_FUND_CODE else f'https://m.gtfund.com/cochin/etf/download/{key}/{{yyyymmdd}}')
    return {'csi':{'id':'graph-csi-'+index,'kind':'csi-members','indexCode':index,'fundCode':None,
        'subscriptionCode':None,'tradingDate':None,
        'url':f'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{index}cons.xls'},
        'pcfTemplate':{'indexCode':index,'fundCode':fund,'downloadResourceKey':key,
            'urlTemplate':template}}


def build_fund_graph_intent(catalog,code,source_contract,now):
    try:
        at=instant(now);scope=next(s for s in catalog.catalog_member_policy.memberGraphs if s.code==code)
        doc=next(d for d in catalog.catalog_member_policy.discoveryDocuments if d.id==scope.discoveryDocumentId)
        target=next(t for t in catalog.targets if t.code==code)
        if source_contract['plan']['document']!=doc.model_dump():raise ValueError()
        t={'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint}
        value={'schemaVersion':1,'firstBusinessDate':at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'createdAt':at.isoformat(),'catalogFingerprint':catalog.fingerprint,'target':t,'sourceContract':deepcopy(source_contract),
            'indexCode':doc.indexCode,'limits':{k:getattr(catalog.official_policy,k) for k in GRAPH_BOUNDS},
            'sources':_sources(doc.indexCode,doc.fundCode,source_contract['normalized']['downloadResourceKey'])}
        value['fingerprint']=digest(value);validate_fund_graph_intent(value);return value
    except Exception:raise ValueError('FUND_MEMBER_GRAPH_INTENT_INVALID') from None


def validate_fund_graph_intent(plan):
    try:
        if (type(plan) is not dict or set(plan)!={'schemaVersion','firstBusinessDate','createdAt','catalogFingerprint','target','sourceContract','indexCode','limits','sources','fingerprint'}
            or type(plan['schemaVersion']) is not int or plan['schemaVersion']!=1 or not _sha(plan['catalogFingerprint'])):raise ValueError()
        at=_stamp(plan['createdAt'])
        if _day(plan['firstBusinessDate'])!=at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():raise ValueError()
        t=plan['target'];p=plan['sourceContract']['plan']
        if (type(t) is not dict or set(t)!={'code','kind','label','sourceIndexNameHint'} or t['kind']!='etf'
            or t!=p['target']):raise ValueError()
        doc=_contract(plan['sourceContract'],t,plan['catalogFingerprint'],at)
        if plan['indexCode']!=doc.indexCode or plan['sources']!=_sources(doc.indexCode,doc.fundCode,plan['sourceContract']['normalized']['downloadResourceKey']):raise ValueError()
        limits=plan['limits']
        if type(limits) is not dict or set(limits)!=set(GRAPH_BOUNDS):raise ValueError()
        for k,maximum in GRAPH_BOUNDS.items():
            if type(limits[k]) is not int or not (0 if k=='maxRetries' else 1)<=limits[k]<=maximum:raise ValueError()
        if digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:raise ValueError()
        return plan
    except Exception:raise ValueError('FUND_MEMBER_GRAPH_INTENT_INVALID') from None


def fund_graph_run_key(intent,kind):
    validate_fund_graph_intent(intent)
    if kind not in ('csi-members','fund-pcf'):raise ValueError('FUND_MEMBER_GRAPH_SOURCE_INVALID')
    return str(uuid5(NAMESPACE,intent['target']['code']+':'+kind))


def _csi_receipt(receipt,intent,as_of):
    _receipt_shape(receipt);p=receipt['plan']
    if type(p) is not dict or p.get('document',{}).get('kind')!='csi-members' or p.get('csiReceipt') is not None:raise ValueError()
    validate_fund_graph_leaf(p)
    v=receipt['normalized']
    if (p['intent']!=intent or p['document']['kind']!='csi-members'
        or receipt['runId']!=fund_graph_run_key(intent,'csi-members')
        or _stamp(receipt['retrievedAt'])<_stamp(p['reservedAt']) or _stamp(receipt['completedAt'])>as_of
        or set(v)!={'schemaVersion','indexCode','indexName','indexEnglishName','asOfDate','members','memberCount','rawSha256'}
        or v['schemaVersion']!='1.0' or v['indexCode']!=intent['indexCode']
        or any(type(v[k]) is not str or not 1<=len(v[k])<=2000 for k in ('indexName','indexEnglishName'))
        or _day(v['asOfDate'])>_stamp(p['reservedAt']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()):raise ValueError()
    members=v['members']
    if (type(members) is not list or not 1<=len(members)<=intent['limits']['maxSnapshotMembers']
        or any(type(s) is not str or re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)',s) is None for s in members)
        or members!=sorted(set(members)) or len({s[:6] for s in members})!=len(members)
        or type(v['memberCount']) is not int or v['memberCount']!=len(members)):raise ValueError()
    return v


def build_fund_graph_leaf(intent,kind,now,csi_receipt=None):
    try:
        validate_fund_graph_intent(intent);at=instant(now)
        if kind=='csi-members':
            if csi_receipt is not None:raise ValueError()
            doc=intent['sources']['csi']
        elif kind=='fund-pcf':
            normalized=_csi_receipt(csi_receipt,intent,at);date=_day(normalized['asOfDate']);template=intent['sources']['pcfTemplate']
            doc={'id':'graph-pcf-'+template['fundCode'],'kind':'fund-pcf','indexCode':intent['indexCode'],
                'fundCode':template['fundCode'],'subscriptionCode':template['downloadResourceKey'],'tradingDate':date,
                'url':template['urlTemplate'].format(yyyymmdd=date.replace('-',''))}
        else:raise ValueError()
        value={'schemaVersion':1,'intent':deepcopy(intent),'reservedAt':at.isoformat(),'document':deepcopy(doc),'csiReceipt':deepcopy(csi_receipt)}
        value['fingerprint']=digest(value);validate_fund_graph_leaf(value);return value
    except Exception:raise ValueError('FUND_MEMBER_GRAPH_LEAF_INVALID') from None


def validate_fund_graph_leaf(plan):
    try:
        if (type(plan) is not dict or set(plan)!={'schemaVersion','intent','reservedAt','document','csiReceipt','fingerprint'}
            or type(plan['schemaVersion']) is not int or plan['schemaVersion']!=1):raise ValueError()
        intent=validate_fund_graph_intent(plan['intent']);at=_stamp(plan['reservedAt']);doc=OfficialSource.model_validate(plan['document'])
        if at<_stamp(intent['createdAt']):raise ValueError()
        if doc.kind=='csi-members':
            if plan['document']!=intent['sources']['csi'] or plan['csiReceipt'] is not None:raise ValueError()
        elif doc.kind=='fund-pcf':
            normalized=_csi_receipt(plan['csiReceipt'],intent,at);template=intent['sources']['pcfTemplate'];date=normalized['asOfDate']
            expected={'id':'graph-pcf-'+template['fundCode'],'kind':'fund-pcf','indexCode':intent['indexCode'],
                'fundCode':template['fundCode'],'subscriptionCode':template['downloadResourceKey'],'tradingDate':date,
                'url':template['urlTemplate'].format(yyyymmdd=date.replace('-',''))}
            if plan['document']!=expected:raise ValueError()
        else:raise ValueError()
        if digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:raise ValueError()
        return plan
    except Exception:raise ValueError('FUND_MEMBER_GRAPH_LEAF_INVALID') from None
