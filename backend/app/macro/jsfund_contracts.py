"""嘉实新官网芯片ETF闭合公开合同；不借用旧JSP和用户会话。"""
from datetime import date
from decimal import Decimal
from hashlib import sha256
from html import unescape
import re
from app.macro.extensions.client import decode_json

FUND_CODE='588200'
INDEX_CODE='000685'
INDEX_NAME='上证科创板芯片指数'
URL='https://www.jsfund.cn/servlet/json'
PRODUCT_ID='1963'


def canonical_index_name(fund,hint):
    if fund!=FUND_CODE or hint not in ('科创芯片指数',INDEX_NAME):raise ValueError('JSFUND_INDEX_ALIAS_INVALID')
    return INDEX_NAME


def public_parameters(kind,day=None):
    if kind=='fund-mapping':return {'funcNo':'741011','product_id':PRODUCT_ID}
    if kind!='fund-pcf' or type(day) is not str or date.fromisoformat(day).isoformat()!=day:raise ValueError('JSFUND_QUERY_INVALID')
    return {'funcNo':'742002','product_code':FUND_CODE,'query_date':day}


def request_form(kind,day=None):
    p=public_parameters(kind,day)
    # 官方serviceConstants公开协议常量，与用户登录身份无关。
    return p|{'channel_source':'','sessionId':'2'} if kind=='fund-mapping' else p


def _json(body):
    if type(body) is not bytes or not 1<=len(body)<=8388608:raise ValueError()
    v=decode_json(body)
    if type(v) is not dict or v.get('error_no')!='0' or v.get('error_info')!='':raise ValueError()
    return v


def parse_jsfund_contract(body,fund_code,expected_index_name):
    try:
        canonical_index_name(fund_code,expected_index_name);v=_json(body)
        if set(v)!={'error_no','error_info','dsName','results'} or v['dsName']!=['results'] or type(v['results']) is not list or len(v['results'])!=1:raise ValueError()
        p=v['results'][0]
        if (type(p) is not dict or p.get('product_id')!=PRODUCT_ID or p.get('product_code')!=FUND_CODE
            or p.get('product_name')!='嘉实上证科创板芯片交易型开放式指数证券投资基金' or p.get('manager_id')!='嘉实基金管理有限公司'
            or type(p.get('parag_content')) is not str or not 1<=len(p['parag_content'])<=20000):raise ValueError()
        text=unescape(re.sub('<[^>]*>',' ',p['parag_content']))
        names=re.findall(r'本基金跟踪([^，。\n]+指数)[，。]',text)
        if not names or set(names)!={INDEX_NAME}:raise ValueError()
        return {'schemaVersion':'1.0','fundCode':fund_code,'fundName':p['product_name'],'trackingIndexName':INDEX_NAME,
            'rawSha256':sha256(body).hexdigest(),'downloadResourceKey':FUND_CODE}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None


def parse_jsfund_pcf(body,fund_code,trading_date,*,max_members=10000):
    try:
        if fund_code!=FUND_CODE or type(max_members) is not int or not 1<=max_members<=10000:raise ValueError()
        public_parameters('fund-pcf',trading_date);v=_json(body)
        if (set(v)!={'error_no','error_info','dsName','stock_info','pcf_info'} or v['dsName']!=['stock_info','pcf_info']
            or type(v['pcf_info']) is not list or len(v['pcf_info'])!=1 or type(v['stock_info']) is not list
            or not 1<=len(v['stock_info'])<=max_members):raise ValueError()
        p=v['pcf_info'][0];compact=trading_date.replace('-','')
        expected={'FundID':FUND_CODE,'FundInstrumentID':FUND_CODE,'TradingDay':compact,'RecordNumber':str(len(v['stock_info'])),
            'Recordnum':str(len(v['stock_info'])),'SecurityIDSource':'101','FundManagementCompany':'嘉实基金管理有限公司',
            'FundName':'嘉实上证科创板芯片ETF','pcfFileUrl':f'http://download.jsfund.cn/pcf/{FUND_CODE}/{trading_date[:4]}/ssepcf_{FUND_CODE}_{compact}.xml'}
        if type(p) is not dict or any(p.get(k)!=x for k,x in expected.items()):raise ValueError()
        markets={'101':('SH','上海市场'),'102':('SZ','深圳市场'),'106':('BJ','北京市场')};members=set();qualified=[]
        for row in v['stock_info']:
            if (type(row) is not dict or type(row.get('secucode')) is not str or re.fullmatch('[0-9]{6}',row['secucode']) is None
                or row['secucode'] in members or type(row.get('secuname')) is not str or not row['secuname'].strip()
                or type(row.get('exchangeCode')) is not str or row['exchangeCode'] not in markets
                or row.get('exchangeName')!=markets[row['exchangeCode']][1] or row.get('secusubflag') not in ('0','1','2')):raise ValueError()
            number=row.get('secuno')
            if type(number) is not str or not 1<=len(number)<=40 or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',number) is None:raise ValueError()
            q=Decimal(number)
            if not q.is_finite() or not 0<=q<Decimal('1e18'):raise ValueError()
            members.add(row['secucode']);qualified.append(row['secucode']+'.'+markets[row['exchangeCode']][0])
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,'memberCodes':sorted(members),
            'qualifiedMemberCodes':sorted(qualified),'memberCount':len(members),
            'membershipBasis':'published-pcf-all-components-not-fund-holdings','rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('JSFUND_PCF_INVALID') from None
