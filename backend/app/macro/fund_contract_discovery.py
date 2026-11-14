"""官方基金下载合同核查：只有元数据，不能作完整成员见证。"""
from datetime import date
from html.parser import HTMLParser
import json
import re
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from app.macro.official_member_policy import OfficialSource
from app.macro.official_members import parse_fund_mapping
from app.macro.extensions.plan import digest
from app.macro.extensions.store import instant
from app.macro.official_member_budget import DISCOVERY_JOB_PREFIX

PREFIX=DISCOVERY_JOB_PREFIX
NAMESPACE=UUID('bb2b36ba-e908-59ad-8b3a-5f202e284615')
BOUNDS={'maxHttpPerRun':4,'maxHttpPerDay':4,'requestsPerMinute':10,'requestTimeoutSeconds':20,'maxRetries':0,'maxResponseBytes':8388608}


def build_discovery_plan(catalog,document_id,now):
    try:
        doc=next(d for d in catalog.catalog_member_policy.discoveryDocuments if d.id==document_id)
        target=next(t for t in catalog.targets if t.code[:6]==doc.fundCode)
        value={'schemaVersion':1,'firstBusinessDate':instant(now).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'catalogFingerprint':catalog.fingerprint,'document':doc.model_dump(),
            'target':{'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint},
            'limits':{k:getattr(catalog.official_policy,k) for k in BOUNDS}}
        value['fingerprint']=digest(value);validate_discovery_plan(value);return value
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_PLAN_INVALID') from None


def validate_discovery_plan(plan):
    try:
        if (type(plan) is not dict or set(plan)!={'schemaVersion','firstBusinessDate','catalogFingerprint','document','target','limits','fingerprint'}
            or type(plan['schemaVersion']) is not int or plan['schemaVersion']!=1
            or type(plan['firstBusinessDate']) is not str or date.fromisoformat(plan['firstBusinessDate']).isoformat()!=plan['firstBusinessDate']
            or type(plan['catalogFingerprint']) is not str or re.fullmatch('[0-9a-f]{64}',plan['catalogFingerprint']) is None):raise ValueError()
        doc=OfficialSource.model_validate(plan['document']);t=plan['target'];limits=plan['limits']
        if (doc.kind!='fund-mapping' or type(t) is not dict or set(t)!={'code','kind','label','sourceIndexNameHint'}
            or t['kind']!='etf' or t['code'] not in (doc.fundCode+'.SH',doc.fundCode+'.SZ')
            or type(t['label']) is not str or not 1<=len(t['label'])<=160
            or type(t['sourceIndexNameHint']) is not str or not t['sourceIndexNameHint'].endswith('指数')
            or not 3<=len(t['sourceIndexNameHint'])<=100 or type(limits) is not dict or set(limits)!=set(BOUNDS)):raise ValueError()
        if doc.tradingDate is not None and doc.tradingDate>plan['firstBusinessDate']:raise ValueError()
        for k,maximum in BOUNDS.items():
            minimum=0 if k=='maxRetries' else 1
            if type(limits[k]) is not int or not minimum<=limits[k]<=maximum:raise ValueError()
        if digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:raise ValueError()
        return doc
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_PLAN_INVALID') from None


def run_key(plan):
    doc=validate_discovery_plan(plan)
    return str(uuid5(NAMESPACE,doc.fundCode))


class _ResourceSelect(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.ignored=0;self.active=False;self.selects=0;self.values=[];self.scripts=[];self.script=None

    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):
            self.ignored+=1
            if tag=='script':self.script=[]
        if self.ignored:return
        attrs=dict(attrs)
        if tag=='select':
            self.active='J_etf_fundcode' in (attrs.get('class') or '').split()
            if self.active:self.selects+=1
        if tag=='option' and self.active:self.values.append(attrs.get('value'))

    def handle_endtag(self,tag):
        if tag in ('script','style'):
            if tag=='script' and self.script is not None:self.scripts.append(''.join(self.script));self.script=None
            self.ignored=max(0,self.ignored-1);return
        if not self.ignored and tag=='select':self.active=False

    def handle_data(self,data):
        if self.script is not None:self.script.append(data)


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError()
        value[key]=item
    return value


def parse_fund_contract(body,fund_code,expected_index_name,*,document=None):
    from app.macro.media_contracts import FUND_CODE as MEDIA_CODE,parse_media_contract
    if fund_code==MEDIA_CODE:return parse_media_contract(body,fund_code,expected_index_name,document)
    from app.macro.etf_definition_contracts import PROFILES as DEFINITION_PROFILES,parse_definition_contract
    if fund_code in DEFINITION_PROFILES:return parse_definition_contract(body,fund_code,expected_index_name,document)
    from app.macro.jsfund_contracts import FUND_CODE as CHIP_CODE,parse_jsfund_contract
    if fund_code==CHIP_CODE:return parse_jsfund_contract(body,fund_code,expected_index_name)
    from app.macro.consumer_xml_contracts import FUND_CODE as CONSUMER_CODE,parse_consumer_contract
    if fund_code==CONSUMER_CODE:return parse_consumer_contract(body,fund_code,expected_index_name,document)
    from app.macro.fsfund_contracts import FUND_CODE,parse_fsfund_contract
    if fund_code==FUND_CODE:return parse_fsfund_contract(body,fund_code,expected_index_name)
    from app.macro.nffund_contracts import CONTRACTS,parse_nffund_contract
    if fund_code in CONTRACTS:return parse_nffund_contract(body,fund_code,expected_index_name)
    try:
        value=parse_fund_mapping(body,fund_code)
        if value['trackingIndexName']!=expected_index_name:raise ValueError()
        parser=_ResourceSelect();parser.feed(body.decode('utf-8-sig'));parser.close()
        if (parser.selects!=1 or len(parser.values)!=1 or type(parser.values[0]) is not str
            or re.fullmatch('[0-9]{6}',parser.values[0]) is None):raise ValueError()
        # 只把确定的JSON对象当数据解析，不执行JavaScript；绑定本基金及页面所选资源键。
        scripts=[s for s in parser.scripts if re.match(r'\s*var\s+gtOptions\s*=\s*\{',s)]
        if len(scripts)!=1:raise ValueError()
        script=scripts[0];locations=list(re.finditer(r'^\s*detail\s*:\s*',script,re.MULTILINE))
        fund=re.findall(r'^\s*fundcode\s*:\s*[\"\']([0-9]{6})[\"\']\s*,\s*$',script,re.MULTILINE)
        if len(locations)!=1 or fund!=[fund_code]:raise ValueError()
        detail,_=json.JSONDecoder(object_pairs_hook=_unique).raw_decode(script[locations[0].end():])
        if type(detail) is not dict or detail.get('code')!=fund_code:raise ValueError()
        entries=detail.get('etf')
        if (type(entries) is not list or len(entries)!=1 or type(entries[0]) is not dict
            or entries[0].get('fundCode')!=fund_code or entries[0].get('code')!=parser.values[0]):raise ValueError()
        return {**value,'downloadResourceKey':parser.values[0]}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None
