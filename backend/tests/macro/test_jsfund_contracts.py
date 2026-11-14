from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module
import json
from urllib.parse import parse_qs

import httpx
import pytest
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import value as base_value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.official_xls_fixture import workbook

NOW=datetime(2026,10,28,2,tzinfo=timezone.utc)
URL='https://www.jsfund.cn/servlet/json'


def doc(kind='fund-mapping',day='2026-10-27'):
    return {'id':'chip-'+kind,'kind':kind,'indexCode':'000685','fundCode':'588200',
        'subscriptionCode':None if kind=='fund-mapping' else '588200','tradingDate':None if kind=='fund-mapping' else day,'url':URL}


def value():
    v=base_value();v['officialEvidence']['dailyBudgetEnabled']=False
    v['targets'].append({'kind':'etf','code':'588200.SH','label':'科创芯片ETF','sourceIndexNameHint':'科创芯片指数'})
    v['catalogEvidence']['targets'].append({'code':'588200.SH','documents':[]})
    v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True,memberGraphEnabled=True,
        discoveryDocuments=[doc()],memberGraphs=[{'code':'588200.SH','discoveryDocumentId':doc()['id']}])
    return v


def metadata():
    return {'error_no':'0','error_info':'','dsName':['results'],'results':[{
        'product_id':'1963','product_code':'588200','product_name':'嘉实上证科创板芯片交易型开放式指数证券投资基金',
        'manager_id':'嘉实基金管理有限公司','parag_content':'<p>本基金跟踪上证科创板芯片指数，上证科创板芯片指数从科创板上市公司中选取证券。</p>',
        'benchmark':'上证科创板芯片指数收益率'}]}


def pcf():
    return {'error_no':'0','error_info':'','dsName':['stock_info','pcf_info'],
        'pcf_info':[{'FundID':'588200','FundInstrumentID':'588200','TradingDay':'20261027','RecordNumber':'2','Recordnum':'2',
            'SecurityIDSource':'101','FundManagementCompany':'嘉实基金管理有限公司','FundName':'嘉实上证科创板芯片ETF',
            'pcfFileUrl':'http://download.jsfund.cn/pcf/588200/2026/ssepcf_588200_20261027.xml'}],
        'stock_info':[{'secucode':'688002','secuname':'睿创微纳','secuno':'100','exchangeCode':'101','exchangeName':'上海市场','secusubflag':'1'},
            {'secucode':'688005','secuname':'容百科技','secuno':'0','exchangeCode':'101','exchangeName':'上海市场','secusubflag':'2'}]}


def raw(v):return json.dumps(v,ensure_ascii=False).encode()
def mod():return import_module('app.macro.jsfund_contracts')


def test_explicit_current_declaration_keeps_canonical_name_without_changing_catalog_hint():
    from app.macro.fund_contract_discovery import parse_fund_contract
    v=parse_fund_contract(raw(metadata()),'588200','科创芯片指数')
    assert v['trackingIndexName']=='上证科创板芯片指数' and v['downloadResourceKey']=='588200'
    for fund,hint in [('588200','其他芯片指数'),('588201','科创芯片指数')]:
        with pytest.raises(ValueError):mod().canonical_index_name(fund,hint)


@pytest.mark.parametrize('field,bad',[('product_id','1964'),('product_code','588201'),('product_name','其他基金'),
    ('manager_id','其他管理人'),('parag_content','业绩比较基准为上证科创板芯片指数收益率')])
def test_identity_and_benchmark_only_metadata_refused(field,bad):
    v=metadata();v['results'][0][field]=bad
    with pytest.raises(ValueError):mod().parse_jsfund_contract(raw(v),'588200','科创芯片指数')


def test_pcf_binds_full_identity_date_count_market_and_keeps_zero_stock():
    v=mod().parse_jsfund_pcf(raw(pcf()),'588200','2026-10-27',max_members=2)
    assert v['memberCodes']==['688002','688005'] and v['qualifiedMemberCodes']==['688002.SH','688005.SH']
    with pytest.raises(ValueError):mod().parse_jsfund_pcf(raw(pcf()),'588200','2026-10-27',max_members=1)


@pytest.mark.parametrize('field,bad',[('FundID','588201'),('FundInstrumentID','588201'),('TradingDay','20261026'),('RecordNumber','3'),
    ('Recordnum','1'),('SecurityIDSource','102'),('FundManagementCompany','其他公司'),('FundName','其他ETF'),('pcfFileUrl','http://example.org/fake')])
def test_wrong_pcf_root_rejected(field,bad):
    v=pcf();v['pcf_info'][0][field]=bad
    with pytest.raises(ValueError):mod().parse_jsfund_pcf(raw(v),'588200','2026-10-27')


@pytest.mark.parametrize('field,bad',[('secucode','688005'),('secuname',''),('secuno','NaN'),('secuno','1_000'),('secuno','9'*41),
    ('exchangeCode','9999'),('exchangeName','深圳市场'),('secusubflag','9')])
def test_wrong_pcf_stock_rejected(field,bad):
    v=pcf();v['stock_info'][0][field]=bad
    with pytest.raises(ValueError):mod().parse_jsfund_pcf(raw(v),'588200','2026-10-27')


def test_closed_source_and_public_parameters():
    from app.macro.nffund_contracts import request_options
    from app.macro.official_member_policy import OfficialSource
    assert request_options(doc())==('POST',{'form':{'funcNo':'741011','product_id':'1963','channel_source':'','sessionId':'2'},'gzip_response_allowed':True})
    assert request_options(doc('fund-pcf'))==('POST',{'form':{'funcNo':'742002','product_code':'588200','query_date':'2026-10-27'},'gzip_response_allowed':True})
    for field,bad in [('indexCode','000686'),('url',URL+'?fund=588200'),('subscriptionCode','588201')]:
        v=doc('fund-pcf');v[field]=bad
        with pytest.raises(ValueError):OfficialSource.model_validate(v)


def test_normal_three_originals_graph_alias_date_and_zero_reentry(database,tmp_path,monkeypatch):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    from app.macro.enterprise_official_targets import valid_witness
    _,factory=database;c=load(tmp_path,value());d=import_module('app.macro.jobs.fund_contract_runtime');g=import_module('app.macro.jobs.fund_member_graph_runtime')
    monkeypatch.setattr(d,'load_enterprise_target_catalog',lambda:c);monkeypatch.setattr(g,'load_enterprise_target_catalog',lambda:c)
    calls=[]
    def handle(request):
        calls.append(request)
        if request.method=='GET':
            return response(200,content=workbook([['20261027','000685','科创芯片','CHIP','688002','睿创微纳','RAY','上海证券交易所','Shanghai Stock Exchange'],
                ['20261027','000685','科创芯片','CHIP','688005','容百科技','RONBAY','上海证券交易所','Shanghai Stock Exchange']]))
        assert str(request.url)==URL
        params=parse_qs(request.content.decode(),keep_blank_values=True)
        if params['funcNo']==['741011']:return response(200,content=raw(metadata()))
        assert params=={'funcNo':['742002'],'product_code':['588200'],'query_date':['2026-10-27']}
        return response(200,content=raw(pcf()))
    t=httpx.MockTransport(handle)
    a=d.execute_fund_contracts(factory,c,clock=lambda:NOW,transport=t)
    assert a['status']=='completed' and a['supplierHttp']==1,a
    b=g.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(seconds=1),transport=t)
    assert b['configuredGraphsCompleted'] and b['supplierHttp']==2,b
    w=load_catalog_member_witnesses(factory,NOW+timedelta(seconds=2),c)[0]
    assert w['trackingIndexName']=='上证科创板芯片指数' and w['members']==['688002.SH','688005.SH']
    assert w['documents'][1]['requestParameters']=={'funcNo':'741011','product_id':'1963'}
    assert w['documents'][2]['requestParameters']=={'funcNo':'742002','product_code':'588200','query_date':'2026-10-27'}
    for index,field,bad in [(1,'product_id','1964'),(2,'query_date','2026-10-26'),(2,'product_code','588201')]:
        v=deepcopy(w);v['documents'][index]['requestParameters'][field]=bad
        assert not valid_witness(v,'588200.SH','etf',NOW.date())
    assert d.execute_fund_contracts(factory,c,clock=lambda:NOW+timedelta(days=1),transport=t)['supplierHttp']==0
    assert g.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(days=1),transport=t)['supplierHttp']==0
    assert len(calls)==3
