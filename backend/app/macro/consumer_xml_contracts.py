"""汇添富消费ETF规范PCF：根拟合指数声明与全部成分，现金不冒充股票。"""
from datetime import date
from decimal import Decimal
from hashlib import sha256
import re
from xml.etree import ElementTree as ET

FUND_CODE='159928'
INDEX_CODE='000932'
INDEX_NAME='中证主要消费指数'
PCF_TEMPLATE='https://www.99fund.com/cgi-bin/fundproduct/EtfStockAction?function=DownLoad&fundId=159928&tradingDay={yyyymmdd}&urlType=1'
NS='{http://ts.szse.cn/Fund}'


def pcf_url(day):
    if type(day) is not str or date.fromisoformat(day).isoformat()!=day:raise ValueError('CONSUMER_DATE_INVALID')
    return PCF_TEMPLATE.format(yyyymmdd=day.replace('-',''))


def statement_day(url):
    if type(url) is not str:raise ValueError('CONSUMER_URL_INVALID')
    match=re.fullmatch(re.escape(PCF_TEMPLATE).replace(re.escape('{yyyymmdd}'),'([0-9]{8})'),url)
    if match is None:raise ValueError('CONSUMER_URL_INVALID')
    compact=match[1];day=compact[:4]+'-'+compact[4:6]+'-'+compact[6:]
    if pcf_url(day)!=url:raise ValueError('CONSUMER_URL_INVALID')
    return day


def _fields(node):
    value={}
    for child in node:
        if not child.tag.startswith(NS) or child.tag in value:raise ValueError()
        value[child.tag]=child
    return value


def _text(fields,name):
    node=fields[NS+name]
    if len(node) or node.text is None or not node.text.strip() or len(node.text)>160:raise ValueError()
    return node.text.strip()


def parse_consumer_xml_pcf(body,fund_code,trading_date,*,max_members=10000):
    try:
        if (type(body) is not bytes or not 1<=len(body)<=8388608 or fund_code!=FUND_CODE
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        pcf_url(trading_date)
        text=body.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):raise ValueError()
        root=ET.fromstring(text)
        if root.tag!=NS+'PCFFile' or root.attrib:raise ValueError()
        f=_fields(root)
        expected={'Version':'1.0','SecurityID':FUND_CODE,'SecurityIDSource':'102','Symbol':'消费ETF汇添富',
            'FundManagementCompany':'汇添富基金管理股份有限公司','UnderlyingSecurityID':INDEX_CODE,
            'UnderlyingSecurityIDSource':'102','TradingDay':trading_date.replace('-','')}
        if any(_text(f,k)!=v for k,v in expected.items()):raise ValueError()
        counts=[]
        for key in ('RecordNum','TotalRecordNum'):
            s=_text(f,key)
            if re.fullmatch(r'[0-9]{1,5}',s) is None:raise ValueError()
            counts.append(int(s))
        container=f[NS+'Components'];rows=list(container)
        if (container.attrib or len(rows)!=counts[1] or not 1<=len(rows)<=max_members+1
            or any(row.tag!=NS+'Component' or row.attrib for row in rows)):raise ValueError()
        # 采用深交所1.29市场码；当前A股完整集合仅接受明确SH/SZ/BJ，不猜旧版本104。
        markets={'101':'SH','102':'SZ','106':'BJ'};codes=set();qualified=[];cash=[];sz_count=0
        for row in rows:
            c=_fields(row);code=_text(c,'UnderlyingSecurityID');name=_text(c,'UnderlyingSymbol')
            market=_text(c,'UnderlyingSecurityIDSource');number=_text(c,'ComponentShare');flag=_text(c,'SubstituteFlag')
            if (re.fullmatch('[0-9]{6}',code) is None or code in codes or market not in markets
                or flag not in ('0','1','2','3','4','5','6','7','8') or not 1<=len(number)<=40
                or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',number) is None):raise ValueError()
            quantity=Decimal(number)
            if not quantity.is_finite() or not 0<=quantity<Decimal('1e18'):raise ValueError()
            codes.add(code);sz_count+=market=='102'
            if code=='159900' or name=='申赎现金':
                if (code,name,market,quantity,flag)!=('159900','申赎现金','102',Decimal(0),'2'):raise ValueError()
                cash.append('159900.SZ')
            else:qualified.append(code+'.'+markets[market])
        if sz_count!=counts[0] or not 1<=len(qualified)<=max_members:raise ValueError()
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,
            'memberCodes':sorted(s[:6] for s in qualified),'qualifiedMemberCodes':sorted(qualified),
            'memberCount':len(qualified),'componentRecordCount':len(rows),'excludedCashComponents':cash,
            'membershipBasis':'published-pcf-all-components-not-fund-holdings','rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('CONSUMER_XML_PCF_INVALID') from None


def parse_consumer_contract(body,fund_code,expected_index_name,document):
    try:
        from app.macro.official_member_policy import OfficialSource
        doc=OfficialSource.model_validate(document)
        if doc.kind!='fund-mapping' or doc.fundCode!=fund_code or expected_index_name!=INDEX_NAME:raise ValueError()
        value=parse_consumer_xml_pcf(body,fund_code,doc.tradingDate)
        return {'schemaVersion':'1.0','fundCode':fund_code,'fundName':'消费ETF汇添富','trackingIndexName':INDEX_NAME,
            'rawSha256':value['rawSha256'],'downloadResourceKey':FUND_CODE}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None
