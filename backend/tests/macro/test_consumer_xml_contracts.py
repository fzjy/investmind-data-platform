from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module

import httpx
import pytest

from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import value as base_value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.official_xls_fixture import workbook

NOW=datetime(2026,10,28,2,tzinfo=timezone.utc)
TEMPLATE='https://www.99fund.com/cgi-bin/fundproduct/EtfStockAction?function=DownLoad&fundId=159928&tradingDay={day}&urlType=1'


def doc(kind='fund-mapping',day='2026-10-28'):
    return {'id':'consumer-'+kind,'kind':kind,'indexCode':'000932','fundCode':'159928',
        'subscriptionCode':'159928' if kind=='fund-pcf' else None,'tradingDate':day,'url':TEMPLATE.format(day=day.replace('-',''))}


def value():
    v=base_value();v['officialEvidence']['dailyBudgetEnabled']=False
    v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True,memberGraphEnabled=True,
        discoveryDocuments=[doc()],memberGraphs=[{'code':'159928.SZ','discoveryDocumentId':doc()['id']}])
    return v


def component(code,name,market,quantity='100.00',flag='1'):
    return f'''<Component><UnderlyingSecurityID>{code}</UnderlyingSecurityID><UnderlyingSecurityIDSource>{market}</UnderlyingSecurityIDSource>
    <UnderlyingSymbol>{name}</UnderlyingSymbol><ComponentShare>{quantity}</ComponentShare><SubstituteFlag>{flag}</SubstituteFlag></Component>'''


def body(day='20261028'):
    rows=component('159900','申赎现金','102','0.00','2')+component('000568','泸州老窖','102')+component('600519','贵州茅台','101','0.00','2')
    return f'''<?xml version="1.0" encoding="UTF-8"?><PCFFile xmlns="http://ts.szse.cn/Fund"><Version>1.0</Version>
    <SecurityID>159928</SecurityID><SecurityIDSource>102</SecurityIDSource><Symbol>消费ETF汇添富</Symbol>
    <FundManagementCompany>汇添富基金管理股份有限公司</FundManagementCompany><UnderlyingSecurityID>000932</UnderlyingSecurityID>
    <UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource><TradingDay>{day}</TradingDay><RecordNum>2</RecordNum>
    <TotalRecordNum>3</TotalRecordNum><Components>{rows}</Components></PCFFile>'''.encode()


def module():return import_module('app.macro.consumer_xml_contracts')


def test_root_explicit_index_code_is_a_current_mapping_not_an_inference_from_benchmark():
    from app.macro.fund_contract_discovery import parse_fund_contract
    v=parse_fund_contract(body(),'159928','中证主要消费指数',document=doc())
    assert v['fundCode']=='159928' and v['trackingIndexName']=='中证主要消费指数' and v['downloadResourceKey']=='159928'
    assert v['fundName']=='消费ETF汇添富'


def test_szse_record_counts_include_exact_cash_marker_but_stock_set_does_not():
    v=module().parse_consumer_xml_pcf(body(),'159928','2026-10-28')
    assert v['memberCount']==2 and v['memberCodes']==['000568','600519']
    assert v['qualifiedMemberCodes']==['000568.SZ','600519.SH']
    assert v['componentRecordCount']==3 and v['excludedCashComponents']==['159900.SZ']
    assert v['membershipBasis']=='published-pcf-all-components-not-fund-holdings'


@pytest.mark.parametrize('old,new',[
    ('<SecurityID>159928','<SecurityID>159929'),('<SecurityIDSource>102','<SecurityIDSource>101'),
    ('<UnderlyingSecurityID>000932','<UnderlyingSecurityID>000933'),
    ('<TradingDay>20261028','<TradingDay>20261027'),('<RecordNum>2','<RecordNum>1'),
    ('<TotalRecordNum>3','<TotalRecordNum>2'),('<TotalRecordNum>3','<TotalRecordNum>4'),
    ('<Version>1.0','<Version>2.0'),('汇添富基金管理股份有限公司','其他管理人'),
    ('http://ts.szse.cn/Fund','https://example.org/fake'),
    ('<UnderlyingSecurityID>600519','<UnderlyingSecurityID>000568'),
    ('<ComponentShare>100.00','<ComponentShare>1_000'),
    ('<ComponentShare>100.00','<ComponentShare>NaN'),
    ('<UnderlyingSymbol>申赎现金','<UnderlyingSymbol>某股票'),
    ('<UnderlyingSecurityID>159900','<UnderlyingSecurityID>159901'),
    ('<ComponentShare>0.00','<ComponentShare>1.00'),
    ('<SubstituteFlag>2','<SubstituteFlag>1'),
    ('<UnderlyingSymbol>泸州老窖','<UnderlyingSymbol>'),
])
def test_identity_namespace_count_cash_and_numeric_ambiguity_rejected(old,new):
    raw=body().decode().replace(old,new,1).encode()
    with pytest.raises(ValueError):module().parse_consumer_xml_pcf(raw,'159928','2026-10-28')


def test_duplicate_scalar_and_xml_entity_declarations_are_refused():
    raw=body().replace(b'<SecurityID>159928</SecurityID>',b'<SecurityID>159928</SecurityID><SecurityID>159928</SecurityID>')
    with pytest.raises(ValueError):module().parse_consumer_xml_pcf(raw,'159928','2026-10-28')
    raw=body().replace(b'<PCFFile',b'<!DOCTYPE PCFFile [<!ENTITY x "fake">]><PCFFile',1)
    with pytest.raises(ValueError):module().parse_consumer_xml_pcf(raw,'159928','2026-10-28')


def test_member_limit_counts_stocks_and_does_not_silently_delete_zero_quantity_stock():
    assert module().parse_consumer_xml_pcf(body(),'159928','2026-10-28',max_members=2)['memberCount']==2
    with pytest.raises(ValueError):module().parse_consumer_xml_pcf(body(),'159928','2026-10-28',max_members=1)


def test_metadata_cannot_be_later_than_real_first_business_day(tmp_path):
    from app.macro.fund_contract_discovery import build_discovery_plan
    c=load(tmp_path,value());build_discovery_plan(c,doc()['id'],NOW)
    with pytest.raises(ValueError):build_discovery_plan(c,doc()['id'],NOW-timedelta(days=1))


@pytest.mark.parametrize('mutation',['url','index','date','key','fund'])
def test_closed_source_rejects_arbitrary_downloads_and_date_parameter_mismatch(mutation):
    from app.macro.official_member_policy import OfficialSource
    d=doc('fund-pcf');OfficialSource.model_validate(d)
    if mutation=='url':d['url']+='&redirect=1'
    elif mutation=='index':d['indexCode']='000933'
    elif mutation=='date':d['tradingDate']='2026-10-27'
    elif mutation=='key':d['subscriptionCode']='159929'
    else:d['fundCode']='159929'
    with pytest.raises(ValueError):OfficialSource.model_validate(d)


def test_normal_graph_uses_csi_actual_day_and_never_uses_research_response_as_raw(database,tmp_path,monkeypatch):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    from app.macro.enterprise_official_targets import valid_witness
    _,factory=database;c=load(tmp_path,value());discovery=import_module('app.macro.jobs.fund_contract_runtime');graph=import_module('app.macro.jobs.fund_member_graph_runtime')
    monkeypatch.setattr(discovery,'load_enterprise_target_catalog',lambda:c);monkeypatch.setattr(graph,'load_enterprise_target_catalog',lambda:c)
    calls=[]
    def handle(request):
        calls.append(str(request.url));assert request.method=='GET'
        if str(request.url)==doc()['url']:return response(200,content=body())
        if request.url.path.endswith('cons.xls'):
            return response(200,content=workbook([
                ['20261027','000932','中证消费','CSI Consumer Staples','000568','泸州老窖','LUZHOU','深圳证券交易所','Shenzhen Stock Exchange'],
                ['20261027','000932','中证消费','CSI Consumer Staples','600519','贵州茅台','MOUTAI','上海证券交易所','Shanghai Stock Exchange']]))
        assert str(request.url)==doc('fund-pcf','2026-10-27')['url']
        return response(200,content=body('20261027'))
    transport=httpx.MockTransport(handle)
    a=discovery.execute_fund_contracts(factory,c,clock=lambda:NOW,transport=transport)
    assert a['status']=='completed' and a['supplierHttp']==1,a
    b=graph.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(seconds=1),transport=transport)
    assert b['configuredGraphsCompleted'] and b['supplierHttp']==2,b
    witnesses=load_catalog_member_witnesses(factory,NOW+timedelta(seconds=2),c)
    assert len(witnesses)==1 and witnesses[0]['members']==['000568.SZ','600519.SH']
    proof=witnesses[0]
    assert proof['membershipDate']=='2026-10-27' and proof['documents'][1]['url']==doc()['url']
    for mutation in ('day','future-statement','index','uri'):
        bad=deepcopy(proof)
        if mutation=='day':bad['membershipDate']='2026-10-26'
        elif mutation=='future-statement':bad['documents'][1]['url']=doc(day='2026-10-29')['url']
        elif mutation=='index':bad['indexCode']='000933'
        else:bad['documents'][2]['url']+='&extra=1'
        assert not valid_witness(bad,'159928.SZ','etf',NOW.date())
    assert discovery.execute_fund_contracts(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert graph.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert len(calls)==3
