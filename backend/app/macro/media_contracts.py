"""传媒ETF：有日期的发行人PDF声明与上交所实际响应日PCF。"""
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import re
from xml.etree import ElementTree as ET

FUND_CODE='512980'
INDEX_CODE='399971'
INDEX_NAME='中证传媒指数'
STATEMENT_DATE='2026-03-25'
PDF_URL='https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-25/512980_20260325_8HFW.pdf'
PCF_URL='https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode=512980'


def _bounded_pdf_text(body):
    from pypdf import PdfReader,apply_configuration
    if type(body) is not bytes or not 1<=len(body)<=8388608 or not body.startswith(b'%PDF-'):raise ValueError()
    # 所有限制只作用于本次解析上下文，不污染其他源/线程；不执行PDF中的动作或附件。
    with apply_configuration(maximum_declared_stream_length=4194304,array_based_stream_maximum_output_length=4194304,
        zlib_maximum_output_length=4194304,lzw_maximum_output_length=4194304,run_length_maximum_output_length=4194304,
        stream_decoding_work_maximum_length=16777216,stream_parsing_work_maximum_count=100000,
        page_tree_maximum_entries=64,page_tree_maximum_depth=16,array_maximum_element_count=10000,dictionary_maximum_element_count=10000,
        xform_maximum_invocations_per_extraction=100):
        reader=PdfReader(BytesIO(body),strict=True,root_object_recovery_limit=1000)
        try:
            if reader.is_encrypted or not 1<=len(reader.pages)<=8:raise ValueError()
            if sum(len(v) for v in reader.xref.values())>5000:raise ValueError()
            parts=[];size=0
            # 身份与明确投资范围均在前两页，后续费用/风险文字不能替代声明。
            for page in reader.pages[:2]:
                text=page.extract_text();size+=len(text)
                if size>200000:raise ValueError()
                parts.append(text)
            return re.sub(r'\s+','','\n'.join(parts))
        finally:reader.close()


def parse_media_contract(body,fund_code,expected_index_name,document):
    try:
        from app.macro.official_member_policy import OfficialSource
        d=OfficialSource.model_validate(document)
        if d.kind!='fund-mapping' or fund_code!=FUND_CODE or d.fundCode!=fund_code or expected_index_name!=INDEX_NAME:raise ValueError()
        text=_bounded_pdf_text(body)
        title=re.search(r'(广发中证传媒交易型开放式指数证券投资基金)基金产品资料概要更新',text)
        if title is None:raise ValueError()
        start=text.index('一、产品概况');end=text.index('二、基金投资与净值表现',start)
        header=text[:start]
        if (re.findall(r'编制日期：([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日',header)!=[('2026','3','20')]
            or re.findall(r'送出日期：([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日',header)!=[('2026','3','25')]):raise ValueError()
        identity=text[start:end]
        if (re.findall(r'基金代码([0-9]{6})(?![0-9])',identity)!=[FUND_CODE]
            or re.findall(r'基金简称(.+?)基金代码',identity)!=['广发中证传媒ETF']
            or re.findall(r'基金管理人(.+?)基金托管人',identity)!=['广发基金管理有限公司']):raise ValueError()
        declarations=re.findall(r'标的指数（即([^）]+)）',text[end:])
        if not declarations or set(declarations)!={INDEX_NAME}:raise ValueError()
        return {'schemaVersion':'1.0','fundCode':fund_code,'fundName':title[1],'trackingIndexName':declarations[0],
            'downloadResourceKey':FUND_CODE,'rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None


def parse_media_pcf(body,fund_code,trading_date,*,max_members=10000):
    try:
        if (type(body) is not bytes or not 1<=len(body)<=8388608 or fund_code!=FUND_CODE
            or type(trading_date) is not str or date.fromisoformat(trading_date).isoformat()!=trading_date
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        text=body.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):raise ValueError()
        root=ET.fromstring(text)
        if root.tag!='SSEPortfolioCompositionFile' or root.attrib or len(root)>100:raise ValueError()
        def fields(node):
            result={}
            for child in node:
                if child.tag in result or child.attrib:raise ValueError()
                result[child.tag]=child
            return result
        def scalar(f,key):
            n=f[key]
            if len(n) or type(n.text) is not str or not n.text.strip() or len(n.text)>160:raise ValueError()
            return n.text.strip()
        f=fields(root)
        if scalar(f,'FundInstrumentID')!=FUND_CODE or scalar(f,'TradingDay')!=trading_date.replace('-',''):raise ValueError()
        count=scalar(f,'RecordNumber')
        if re.fullmatch('[1-9][0-9]{0,4}',count) is None or int(count)>max_members or len(f['ComponentList'])!=int(count):raise ValueError()
        codes=set();qualified=[];markets={'101':'SH','102':'SZ','106':'BJ'}
        for row in f['ComponentList']:
            if row.tag!='Component' or row.attrib or len(row)>40:raise ValueError()
            r=fields(row);code=scalar(r,'InstrumentID');name=scalar(r,'InstrumentName');number=scalar(r,'Quantity');market=scalar(r,'UnderlyingSecurityID')
            if (re.fullmatch('[0-9]{6}',code) is None or code in codes or code=='159900' or name=='申赎现金' or market not in markets
                or scalar(r,'SubstitutionFlag') not in ('0','1','2') or not 1<=len(number)<=40
                or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',number) is None):raise ValueError()
            q=Decimal(number)
            if not q.is_finite() or not 0<=q<Decimal('1e18'):raise ValueError()
            codes.add(code);qualified.append(code+'.'+markets[market])
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,'memberCodes':sorted(codes),
            'qualifiedMemberCodes':sorted(qualified),'memberCount':len(codes),
            'membershipBasis':'published-pcf-all-components-not-fund-holdings','rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('MEDIA_PCF_INVALID') from None
