from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module
from io import BytesIO
import httpx
import pytest
from pypdf import PdfReader,PdfWriter
from pypdf.generic import ArrayObject,DictionaryObject,NameObject,NumberObject,TextStringObject,DecodedStreamObject

from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import value as base_value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.official_xls_fixture import workbook

NOW=datetime(2026,10,28,12,tzinfo=timezone.utc)
PDF_URL='https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-25/512980_20260325_8HFW.pdf'
PCF_URL='https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode=512980'
TEXT='''广发中证传媒交易型开放式指数证券投资基金基金产品资料概要更新
编制日期：2026年3月20日 送出日期：2026年3月25日
一、产品概况
基金简称 广发中证传媒ETF 基金代码 512980
基金管理人 广发基金管理有限公司 基金托管人 测试托管行
二、基金投资与净值表现
投资范围 本基金主要投资于标的指数（即中证传媒指数）的成份股。
业绩比较基准 同期中证传媒指数收益率'''


def pdf(text=TEXT,*,pages=1,encrypted=False):
    """合成中文PDF而非模拟extract_text，覆盖真实字体/内容流解析。"""
    writer=PdfWriter();chars=sorted(set(text));cmap='\n'.join('<'+c.encode('utf-16-be').hex()+'><'+c.encode('utf-16-be').hex()+'>' for c in chars)
    stream=DecodedStreamObject();stream.set_data(('begincmap\n1 begincodespacerange\n<0000><ffff>\nendcodespacerange\n'+str(len(chars))+' beginbfchar\n'+cmap+'\nendbfchar\nendcmap').encode())
    descendant=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/CIDFontType2'),NameObject('/BaseFont'):NameObject('/Synthetic'),
        NameObject('/CIDSystemInfo'):DictionaryObject({NameObject('/Registry'):TextStringObject('Adobe'),NameObject('/Ordering'):TextStringObject('Identity'),NameObject('/Supplement'):NumberObject(0)})})
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type0'),NameObject('/BaseFont'):NameObject('/Synthetic'),
        NameObject('/Encoding'):NameObject('/Identity-H'),NameObject('/ToUnicode'):writer._add_object(stream),NameObject('/DescendantFonts'):ArrayObject([writer._add_object(descendant)])})
    for _ in range(pages):
        page=writer.add_blank_page(600,800);page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F0'):writer._add_object(font)})})
        data=DecodedStreamObject();data.set_data(('BT /F0 10 Tf 10 700 Td <'+text.encode('utf-16-be').hex()+'> Tj ET').encode())
        page[NameObject('/Contents')]=writer._add_object(data)
    if encrypted:writer.encrypt('unavailable-password')
    buf=BytesIO();writer.write(buf);return buf.getvalue()


def doc(kind='fund-mapping',day='2026-10-28'):
    return {'id':'media-'+kind,'kind':kind,'indexCode':'399971','fundCode':'512980','subscriptionCode':None if kind=='fund-mapping' else '512980',
        'tradingDate':'2026-03-25' if kind=='fund-mapping' else day,'url':PDF_URL if kind=='fund-mapping' else PCF_URL}


def body(day='20261028'):
    return f'''<SSEPortfolioCompositionFile><FundInstrumentID>512980</FundInstrumentID><TradingDay>{day}</TradingDay><RecordNumber>2</RecordNumber><ComponentList>
    <Component><InstrumentID>000156</InstrumentID><InstrumentName>华数传媒</InstrumentName><Quantity>100</Quantity><SubstitutionFlag>1</SubstitutionFlag><UnderlyingSecurityID>102</UnderlyingSecurityID></Component>
    <Component><InstrumentID>600637</InstrumentID><InstrumentName>东方明珠</InstrumentName><Quantity>0</Quantity><SubstitutionFlag>2</SubstitutionFlag><UnderlyingSecurityID>101</UnderlyingSecurityID></Component>
    </ComponentList></SSEPortfolioCompositionFile>'''.encode()


def mod():return import_module('app.macro.media_contracts')


def test_fixture_is_a_real_parseable_chinese_pdf_and_contract_is_explicit():
    b=pdf();assert '基金代码' in PdfReader(BytesIO(b)).pages[0].extract_text()
    from app.macro.fund_contract_discovery import parse_fund_contract
    v=parse_fund_contract(b,'512980','中证传媒指数',document=doc())
    assert v['fundName']=='广发中证传媒交易型开放式指数证券投资基金'
    assert v['trackingIndexName']=='中证传媒指数' and v['downloadResourceKey']=='512980'


@pytest.mark.parametrize('old,new',[
    ('基金代码 512980','基金代码 512981'),('广发中证传媒ETF','其他ETF'),('广发基金管理有限公司','其他基金管理有限公司'),
    ('编制日期：2026年3月20日','编制日期：2025年3月20日'),('送出日期：2026年3月25日','送出日期：2026年3月26日'),
    ('标的指数（即中证传媒指数）','标的指数（即其他指数）'),
    ('投资范围 本基金主要投资于标的指数（即中证传媒指数）的成份股。','投资范围 暂无标的声明。'),
])
def test_pdf_wrong_identity_date_and_benchmark_only_refused(old,new):
    with pytest.raises(ValueError):mod().parse_media_contract(pdf(TEXT.replace(old,new)),'512980','中证传媒指数',doc())


@pytest.mark.parametrize('kind',['encrypted','too-many-pages','not-pdf','too-large','text-over-limit'])
def test_pdf_bounds_and_encryption_refused(kind):
    b=pdf(encrypted=True) if kind=='encrypted' else pdf(pages=9) if kind=='too-many-pages' else b'not pdf' if kind=='not-pdf' else b'%PDF-'+b'x'*8388608 if kind=='too-large' else pdf(TEXT+'X'*200001)
    with pytest.raises(ValueError):mod().parse_media_contract(b,'512980','中证传媒指数',doc())


def test_pdf_parser_context_configuration_restored():
    from pypdf import get_configuration
    previous=get_configuration();mod().parse_media_contract(pdf(),'512980','中证传媒指数',doc())
    assert get_configuration()==previous


def test_pdf_duplicate_header_date_is_ambiguous():
    b=pdf(TEXT.replace('一、产品概况','送出日期：2026年3月26日 一、产品概况'))
    with pytest.raises(ValueError):mod().parse_media_contract(b,'512980','中证传媒指数',doc())


def test_pdf_compressed_content_above_stream_limit_is_refused():
    import zlib
    from pypdf.generic import EncodedStreamObject
    reader=PdfReader(BytesIO(pdf()));writer=PdfWriter();writer.add_page(reader.pages[0])
    stream=EncodedStreamObject();stream[NameObject('/Filter')]=NameObject('/FlateDecode');stream._data=zlib.compress(b' '*4194305)
    writer.pages[0][NameObject('/Contents')]=writer._add_object(stream)
    buf=BytesIO();writer.write(buf)
    with pytest.raises(ValueError):mod().parse_media_contract(buf.getvalue(),'512980','中证传媒指数',doc())


def test_pcf_complete_set_uses_real_market_and_retains_zero_stock():
    p=mod().parse_media_pcf(body(),'512980','2026-10-28',max_members=2)
    assert p['qualifiedMemberCodes']==['000156.SZ','600637.SH'] and p['memberCount']==2
    with pytest.raises(ValueError):mod().parse_media_pcf(body(),'512980','2026-10-28',max_members=1)


@pytest.mark.parametrize('old,new',[
    ('<FundInstrumentID>512980','<FundInstrumentID>512981'),('<TradingDay>20261028','<TradingDay>20261027'),
    ('<RecordNumber>2','<RecordNumber>3'),('<InstrumentID>600637','<InstrumentID>000156'),('<InstrumentID>000156','<InstrumentID>159900'),
    ('<InstrumentName>华数传媒','<InstrumentName>'),('<Quantity>100','<Quantity>NaN'),('<Quantity>100','<Quantity>1_000'),
    ('<Quantity>100','<Quantity>'+'9'*41),('<UnderlyingSecurityID>102','<UnderlyingSecurityID>9999'),('<SubstitutionFlag>1','<SubstitutionFlag>9'),
])
def test_pcf_ambiguities_fail_closed(old,new):
    with pytest.raises(ValueError):mod().parse_media_pcf(body().replace(old.encode(),new.encode()),'512980','2026-10-28')


def test_pcf_duplicate_and_xml_entities_refused():
    for b in [body().replace(b'<TradingDay>',b'<TradingDay>20261028</TradingDay><TradingDay>',1),
        b'<!DOCTYPE root [<!ENTITY x "bad">]>'+body()]:
        with pytest.raises(ValueError):mod().parse_media_pcf(b,'512980','2026-10-28')


def test_latest_get_uri_has_no_fake_date_parameter_and_source_is_closed():
    from app.macro.official_member_policy import OfficialSource
    from app.macro.nffund_contracts import request_options
    assert request_options(doc('fund-pcf'))==('GET',{'gzip_response_allowed':True})
    for field,bad in [('url',PCF_URL+'&date=20261028'),('indexCode','399972'),('subscriptionCode','512981')]:
        d=doc('fund-pcf');d[field]=bad
        with pytest.raises(ValueError):OfficialSource.model_validate(d)


def value():
    v=base_value();v['officialEvidence']['dailyBudgetEnabled']=False
    v['targets'].append({'kind':'etf','code':'512980.SH','label':'传媒ETF','sourceIndexNameHint':'中证传媒指数'})
    v['catalogEvidence']['targets'].append({'code':'512980.SH','documents':[]})
    v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True,memberGraphEnabled=True,
        discoveryDocuments=[doc()],memberGraphs=[{'code':'512980.SH','discoveryDocumentId':doc()['id']}])
    return v


def test_normal_graph_response_identity_binds_actual_day_and_zero_reentry(database,tmp_path,monkeypatch):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    from app.macro.enterprise_official_targets import valid_witness
    _,factory=database;c=load(tmp_path,value());d=import_module('app.macro.jobs.fund_contract_runtime');g=import_module('app.macro.jobs.fund_member_graph_runtime')
    monkeypatch.setattr(d,'load_enterprise_target_catalog',lambda:c);monkeypatch.setattr(g,'load_enterprise_target_catalog',lambda:c)
    calls=[]
    def handle(request):
        calls.append(request);assert request.method=='GET'
        if str(request.url)==PDF_URL:return response(200,content=pdf())
        if str(request.url)==PCF_URL:return response(200,content=body())
        assert request.url.path.endswith('399971cons.xls')
        return response(200,content=workbook([['20261028','399971','传媒','MEDIA','000156','华数传媒','WASU','深圳证券交易所','Shenzhen Stock Exchange'],
            ['20261028','399971','传媒','MEDIA','600637','东方明珠','OPG','上海证券交易所','Shanghai Stock Exchange']]))
    transport=httpx.MockTransport(handle)
    a=d.execute_fund_contracts(factory,c,clock=lambda:NOW,transport=transport);assert a['supplierHttp']==1 and a['status']=='completed',a
    b=g.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(seconds=1),transport=transport);assert b['supplierHttp']==2 and b['configuredGraphsCompleted'],b
    w=load_catalog_member_witnesses(factory,NOW+timedelta(seconds=2),c)[0]
    assert w['documents'][2]['responseIdentity']=={'fundCode':'512980','tradingDate':'2026-10-28'}
    assert 'requestParameters' not in w['documents'][2] and w['membershipDate']=='2026-10-28'
    for kind in ('day','fund','query','extra','missing'):
        bad=deepcopy(w);p=bad['documents'][2]
        if kind=='day':p['responseIdentity']['tradingDate']='2026-10-27'
        elif kind=='fund':p['responseIdentity']['fundCode']='512981'
        elif kind=='query':p['url']+='&date=20261028'
        elif kind=='extra':p['responseIdentity']['extra']='bad'
        else:del p['responseIdentity']
        assert not valid_witness(bad,'512980.SH','etf',NOW.date())
    assert d.execute_fund_contracts(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert g.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert len(calls)==3


def test_latest_pcf_must_not_silently_match_previous_csi_date():
    with pytest.raises(ValueError):mod().parse_media_pcf(body(),'512980','2026-10-27')
