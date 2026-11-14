"""两份已核实的SSE ETFDefinition原件；根指数声明与同日完整证券集合。"""
from datetime import date
from decimal import Decimal
from hashlib import sha256
import re
from xml.etree import ElementTree as ET

PROFILES={'512010':('000913','沪深300医药卫生指数','医药ETF易方达','易方达基金'),
    '515030':('399976','中证新能源汽车指数','新汽车','华夏基金管理有限公司')}
EV_URL='https://fund.chinaamc.com/front/front/out/etf/query/etfDownload'
MEDICINE_TEMPLATE='https://cdn.efunds.com.cn/owch/data/pcf_ims/{yyyymmdd}/etfd_512010_{yyyymmdd}.xml'


def compact_day(day):
    if type(day) is not str or date.fromisoformat(day).isoformat()!=day:raise ValueError('ETF_DEFINITION_DATE_INVALID')
    return day.replace('-','')


def source_url(fund,day):
    compact=compact_day(day)
    if fund=='512010':return MEDICINE_TEMPLATE.format(yyyymmdd=compact)
    if fund=='515030':return EV_URL
    raise ValueError('ETF_DEFINITION_SOURCE_INVALID')


def ev_parameters(day):return {'fileName':'etfd_515030_'+compact_day(day)+'.xml','year':day[:4],'fundCode':'515030'}


def statement_day(fund,url,parameters=None):
    if fund=='512010':
        if type(url) is not str:raise ValueError()
        m=re.fullmatch(r'https://cdn\.efunds\.com\.cn/owch/data/pcf_ims/([0-9]{8})/etfd_512010_\1\.xml',url)
    elif fund=='515030':
        if url!=EV_URL or type(parameters) is not dict or type(parameters.get('fileName')) is not str:raise ValueError()
        m=re.fullmatch(r'etfd_515030_([0-9]{8})\.xml',parameters['fileName'])
    else:raise ValueError()
    if m is None:raise ValueError()
    c=m[1];day=c[:4]+'-'+c[4:6]+'-'+c[6:];compact_day(day)
    if fund=='515030' and parameters!=ev_parameters(day):raise ValueError()
    return day


def parse_definition_pcf(body,fund_code,trading_date,*,max_members=10000):
    try:
        if (type(body) is not bytes or not 1<=len(body)<=8388608 or fund_code not in PROFILES
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        day=compact_day(trading_date);index,_,name,company=PROFILES[fund_code];text=body.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):raise ValueError()
        root=ET.fromstring(text)
        if root.tag!='ETFDefinition' or root.attrib or len(root)>100:raise ValueError()
        def fields(node):
            result={}
            for n in node:
                if n.tag in result or n.attrib:raise ValueError()
                result[n.tag]=n
            return result
        def scalar(f,key):
            n=f[key]
            if len(n) or type(n.text) is not str or not n.text.strip() or len(n.text)>160:raise ValueError()
            return n.text.strip()
        f=fields(root)
        # IS118中的Version是每日递增修订号；不能误作固定schemaVersion。
        if re.fullmatch('[1-9][0-9]{0,7}',scalar(f,'Version')) is None:raise ValueError()
        if any(scalar(f,k)!=v for k,v in {'FundInstrumentID':fund_code,'FundName':name,
            'FundCompanyName':company,'UnderlyingIndex':index,'TradingDay':day}.items()):raise ValueError()
        count=scalar(f,'RecordNumber')
        if re.fullmatch('[1-9][0-9]{0,4}',count) is None or int(count)>max_members:raise ValueError()
        container=f['ComponentList']
        if len(container)!=int(count):raise ValueError()
        members=set();qualified=[];markets={'101':'SH','102':'SZ','106':'BJ'}
        for row in container:
            if row.tag!='Component' or row.attrib or len(row)>40:raise ValueError()
            r=fields(row);code=scalar(r,'InstrumentID');name=scalar(r,'InstrumentName');number=scalar(r,'Quantity')
            market=scalar(r,'UnderlyingSecurityID')
            if (re.fullmatch('[0-9]{6}',code) is None or code=='159900' or name=='申赎现金' or code in members
                or market not in markets or scalar(r,'SubstitutionFlag') not in ('0','1','2')
                or not 1<=len(number)<=40 or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',number) is None):raise ValueError()
            q=Decimal(number)
            if not q.is_finite() or not 0<=q<Decimal('1e18'):raise ValueError()
            members.add(code);qualified.append(code+'.'+markets[market])
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,'memberCodes':sorted(members),
            'qualifiedMemberCodes':sorted(qualified),'memberCount':len(members),
            'membershipBasis':'published-pcf-all-components-not-fund-holdings','rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('ETF_DEFINITION_PCF_INVALID') from None


def parse_definition_contract(body,fund_code,expected_index_name,document):
    try:
        from app.macro.official_member_policy import OfficialSource
        d=OfficialSource.model_validate(document)
        if d.kind!='fund-mapping' or d.fundCode!=fund_code or expected_index_name!=PROFILES[fund_code][1]:raise ValueError()
        p=parse_definition_pcf(body,fund_code,d.tradingDate)
        return {'schemaVersion':'1.0','fundCode':fund_code,'fundName':PROFILES[fund_code][2],
            'trackingIndexName':expected_index_name,'rawSha256':p['rawSha256'],'downloadResourceKey':fund_code}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None
