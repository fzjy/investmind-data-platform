from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module
from urllib.parse import parse_qs
import httpx
import pytest
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import value as base_value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.official_xls_fixture import workbook

NOW=datetime(2026,10,28,2,tzinfo=timezone.utc)
PROFILES={'512010':('000913','沪深300医药卫生指数','医药ETF易方达','易方达基金'),
    '515030':('399976','中证新能源汽车指数','新汽车','华夏基金管理有限公司')}
EV_URL='https://fund.chinaamc.com/front/front/out/etf/query/etfDownload'


def uri(code,day):
    return f'https://cdn.efunds.com.cn/owch/data/pcf_ims/{day.replace("-","")}/etfd_512010_{day.replace("-","")}.xml' if code=='512010' else EV_URL


def doc(code,kind='fund-mapping',day='2026-10-28'):
    return {'id':'definition-'+code+'-'+kind,'kind':kind,'fundCode':code,'indexCode':PROFILES[code][0],
        'subscriptionCode':None if kind=='fund-mapping' else code,'tradingDate':day,'url':uri(code,day)}


def body(code,day='20261028'):
    index,_,name,company=PROFILES[code]
    return f'''<?xml version="1.0" encoding="UTF-8"?><ETFDefinition><Version>1</Version><FundInstrumentID>{code}</FundInstrumentID>
    <FundName>{name}</FundName><FundCompanyName>{company}</FundCompanyName><UnderlyingIndex>{index}</UnderlyingIndex>
    <TradingDay>{day}</TradingDay><RecordNumber>2</RecordNumber><ComponentList>
    <Component><InstrumentID>000538</InstrumentID><InstrumentName>云南白药</InstrumentName><Quantity>100</Quantity><SubstitutionFlag>1</SubstitutionFlag><UnderlyingSecurityID>102</UnderlyingSecurityID></Component>
    <Component><InstrumentID>920185</InstrumentID><InstrumentName>贝特瑞</InstrumentName><Quantity>0</Quantity><SubstitutionFlag>2</SubstitutionFlag><UnderlyingSecurityID>106</UnderlyingSecurityID></Component>
    </ComponentList></ETFDefinition>'''.encode()


def mod():return import_module('app.macro.etf_definition_contracts')


@pytest.mark.parametrize('code',PROFILES)
def test_root_explicit_index_metadata_and_all_component_exchanges(code):
    from app.macro.fund_contract_discovery import parse_fund_contract
    v=parse_fund_contract(body(code),code,PROFILES[code][1],document=doc(code))
    assert v['trackingIndexName']==PROFILES[code][1] and v['downloadResourceKey']==code
    p=mod().parse_definition_pcf(body(code),code,'2026-10-28',max_members=2)
    assert p['qualifiedMemberCodes']==['000538.SZ','920185.BJ'] and p['memberCodes']==['000538','920185']
    with pytest.raises(ValueError):mod().parse_definition_pcf(body(code),code,'2026-10-28',max_members=1)


@pytest.mark.parametrize('code',PROFILES)
@pytest.mark.parametrize('old,new',[
    ('<Version>1','<Version>0'),('<TradingDay>20261028','<TradingDay>20261027'),('<RecordNumber>2','<RecordNumber>1'),
    ('<Quantity>100','<Quantity>NaN'),('<Quantity>100','<Quantity>1_000'),('<Quantity>100','<Quantity>'+'9'*41),
    ('<UnderlyingSecurityID>106','<UnderlyingSecurityID>104'),('<SubstitutionFlag>2','<SubstitutionFlag>9'),
    ('<InstrumentID>920185','<InstrumentID>000538'),('<InstrumentName>云南白药','<InstrumentName>'),
    ('<InstrumentID>000538','<InstrumentID>159900'),
])
def test_bad_version_date_count_security_and_numbers_refused(code,old,new):
    with pytest.raises(ValueError):mod().parse_definition_pcf(body(code).replace(old.encode(),new.encode()),code,'2026-10-28')


@pytest.mark.parametrize('code',PROFILES)
def test_cross_issuer_index_and_entities_are_not_accepted(code):
    b=body(code);other='515030' if code=='512010' else '512010'
    for old,new in [(PROFILES[code][0],PROFILES[other][0]),(code,other),(PROFILES[code][2],'其他基金'),(PROFILES[code][3],'其他管理人'),
        ('<ETFDefinition>','<!DOCTYPE ETFDefinition [<!ENTITY x "fake">]><ETFDefinition>'),
        ('<RecordNumber>2</RecordNumber>','<RecordNumber>2</RecordNumber><RecordNumber>2</RecordNumber>')]:
        with pytest.raises(ValueError):mod().parse_definition_pcf(b.replace(old.encode(),new.encode()),code,'2026-10-28')


@pytest.mark.parametrize('code',PROFILES)
def test_definition_daily_revision_is_not_a_fixed_schema_version(code):
    # SSE IS118: Version是当日递增版本号，不是固定XML格式版本。
    v=mod().parse_definition_pcf(body(code).replace(b'<Version>1',b'<Version>2'),code,'2026-10-28')
    assert v['memberCount']==2
    for version in ('0','-1','1_000','1.0','9'*9):
        with pytest.raises(ValueError):mod().parse_definition_pcf(body(code).replace(b'<Version>1',('<Version>'+version).encode()),code,'2026-10-28')


@pytest.mark.parametrize('code',PROFILES)
def test_closed_sources_and_actual_date_requests(code):
    from app.macro.official_member_policy import OfficialSource
    from app.macro.nffund_contracts import request_options
    d=doc(code,'fund-pcf');OfficialSource.model_validate(d)
    for field,bad in [('url',d['url']+'?extra=1'),('indexCode','000933'),('subscriptionCode','512011')]:
        b=deepcopy(d);b[field]=bad
        with pytest.raises(ValueError):OfficialSource.model_validate(b)
    method,options=request_options(d)
    assert method==('GET' if code=='512010' else 'POST')
    if code=='515030':assert options['form']=={'fileName':'etfd_515030_20261028.xml','year':'2026','fundCode':'515030'}


@pytest.mark.parametrize('code',PROFILES)
def test_actual_csi_day_three_originals_and_zero_reentry(database,tmp_path,monkeypatch,code):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    from app.macro.enterprise_official_targets import valid_witness
    _,factory=database;v=base_value();v['officialEvidence']['dailyBudgetEnabled']=False
    v['targets'].append({'kind':'etf','code':code+'.SH','label':'测试ETF','sourceIndexNameHint':PROFILES[code][1]})
    v['catalogEvidence']['targets'].append({'code':code+'.SH','documents':[]})
    v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True,memberGraphEnabled=True,discoveryDocuments=[doc(code)],
        memberGraphs=[{'code':code+'.SH','discoveryDocumentId':doc(code)['id']}])
    c=load(tmp_path,v);d=import_module('app.macro.jobs.fund_contract_runtime');g=import_module('app.macro.jobs.fund_member_graph_runtime')
    monkeypatch.setattr(d,'load_enterprise_target_catalog',lambda:c);monkeypatch.setattr(g,'load_enterprise_target_catalog',lambda:c)
    calls=[]
    def handle(request):
        calls.append(request)
        if request.url.path.endswith('cons.xls'):
            return response(200,content=workbook([['20261027',PROFILES[code][0],'测试','TEST','000538','云南白药','YUNNAN','深圳证券交易所','Shenzhen Stock Exchange'],
                ['20261027',PROFILES[code][0],'测试','TEST','920185','贝特瑞','BETTER','北京证券交易所','Beijing Stock Exchange']]))
        if code=='515030':
            assert request.method=='POST';p=parse_qs(request.content.decode());day=p['fileName'][0][-12:-4]
        else:day=request.url.path[-12:-4]
        assert day in ('20261027','20261028')
        return response(200,content=body(code,day))
    transport=httpx.MockTransport(handle)
    a=d.execute_fund_contracts(factory,c,clock=lambda:NOW,transport=transport);assert a['status']=='completed' and a['supplierHttp']==1,a
    b=g.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(seconds=1),transport=transport);assert b['configuredGraphsCompleted'] and b['supplierHttp']==2,b
    w=load_catalog_member_witnesses(factory,NOW+timedelta(seconds=2),c)[0];assert w['members']==['000538.SZ','920185.BJ']
    assert w['membershipDate']=='2026-10-27' and valid_witness(w,code+'.SH','etf',NOW.date())
    bad=deepcopy(w)
    if code=='515030':bad['documents'][2]['requestParameters']['fileName']='etfd_515030_20261026.xml'
    else:bad['documents'][2]['url']=uri(code,'2026-10-26')
    assert not valid_witness(bad,code+'.SH','etf',NOW.date())
    assert d.execute_fund_contracts(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert g.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert len(calls)==3
