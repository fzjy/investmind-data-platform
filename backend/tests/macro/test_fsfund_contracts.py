from copy import deepcopy
from datetime import timedelta
from importlib import import_module
import json

import httpx
import pytest

from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import value as base_value,NOW
from backend.tests.macro.test_issuer_html_contracts import bank
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.official_xls_fixture import workbook

METADATA='https://www.fsfund.com/fund/512800/fundDetail.shtml'
PCF='https://api.fsfund.com/v2/webzk/queryController/getFundShareInfo'


def doc(kind='fund-mapping',day=None):
    return {'id':'bank-'+kind,'kind':kind,'indexCode':'399986','fundCode':'512800',
        'subscriptionCode':'512800' if kind=='fund-pcf' else None,'tradingDate':day,
        'url':PCF if kind=='fund-pcf' else METADATA}


def value():
    v=base_value();v['officialEvidence']['dailyBudgetEnabled']=False
    v['catalogEvidence'].update(collectionEnabled=True,discoveryEnabled=True,memberGraphEnabled=True,
        discoveryDocuments=[doc()],memberGraphs=[{'code':'512800.SH','discoveryDocumentId':doc()['id']}])
    return v


def components(day='20261023'):
    return {'code':'0000','message':'success','error':False,'success':True,'data':[
        {'fundId':'512800','tradingDay':day,'stockCode':'000001','stockShort':'平安银行','number':1500,'tdbz':'允许','scid':'102'},
        {'fundId':'512800','tradingDay':day,'stockCode':'600000','stockShort':'浦发银行','number':0,'tdbz':'必须','scid':'101'}]}


def body(v=None):return json.dumps(components() if v is None else v,ensure_ascii=False).encode()
def module():return import_module('app.macro.fsfund_contracts')


def test_public_web_signature_matches_the_observed_web_protocol_and_real_query_date():
    value=module().fsfund_request_payload('512800','2026-10-23',NOW)
    assert value['fundCode']=='512800' and value['startDate']=='20261023' and value['netNo']=='web'
    assert value['timestamp']=='1792980000000'
    assert value['signature']=='89efd0303614c138c4eead097b6b13b4'
    with pytest.raises(ValueError):module().fsfund_request_payload('512810','2026-10-23',NOW)
    with pytest.raises(ValueError):module().fsfund_request_payload('512800','2026-02-30',NOW)


def test_bank_statement_uses_scoped_product_identity_and_explicit_index_declaration():
    from app.macro.fund_contract_discovery import parse_fund_contract
    v=parse_fund_contract(bank(),'512800','中证银行指数')
    assert v['trackingIndexName']=='中证银行指数' and v['downloadResourceKey']=='512800'
    with pytest.raises(ValueError):parse_fund_contract(bank(),'512800','中证医药指数')


def test_bank_pcf_preserves_zero_quantity_stock_and_explicit_exchange():
    v=module().parse_fsfund_pcf(body(),'512800','2026-10-23')
    assert v['memberCodes']==['000001','600000'] and v['qualifiedMemberCodes']==['000001.SZ','600000.SH']
    assert v['memberCount']==2 and v['asOfDate']=='2026-10-23'


@pytest.mark.parametrize('mutation',['fund','day','market','duplicate','quantity','boolean-quantity','empty','status','duplicate-json','name','truncated','underscore-quantity','oversize-quantity'])
def test_bank_pcf_rejects_wrong_identity_date_market_and_invalid_or_partial_rows(mutation):
    v=components();rows=v['data']
    if mutation=='fund':rows[0]['fundId']='512810'
    elif mutation=='day':rows[0]['tradingDay']='20261022'
    elif mutation=='market':rows[0]['scid']='999'
    elif mutation=='duplicate':rows[1]['stockCode']=rows[0]['stockCode']
    elif mutation=='quantity':rows[0]['number']=-1
    elif mutation=='boolean-quantity':rows[0]['number']=True
    elif mutation=='empty':v['data']=[]
    elif mutation=='status':v['success']=False
    elif mutation=='name':rows[0]['stockShort']=''
    elif mutation=='truncated':del rows[0]['tradingDay']
    elif mutation=='underscore-quantity':rows[0]['number']='1_500'
    elif mutation=='oversize-quantity':rows[0]['number']='0.'+'0'*10000
    raw=body(v)
    if mutation=='duplicate-json':raw=raw.replace(b'"fundId":',b'"fundId":"512810","fundId":',1)
    with pytest.raises(ValueError):module().parse_fsfund_pcf(raw,'512800','2026-10-23')


def test_bank_pcf_size_limit_is_not_the_development_pool_size():
    with pytest.raises(ValueError):module().parse_fsfund_pcf(body(),'512800','2026-10-23',max_members=1)
    assert module().parse_fsfund_pcf(body(),'512800','2026-10-23',max_members=2)['memberCount']==2


def test_bank_graph_rejects_wrong_exchange_even_if_the_six_digit_sets_match():
    from app.macro.fund_member_graph_store import FundMemberGraphStore
    plan={'intent':{'limits':{'maxSnapshotMembers':10000}},'csiReceipt':{'normalized':{'members':['000001.SH','600000.SH']}}}
    with pytest.raises(ValueError,match='FUND_MEMBER_GRAPH_MEMBER_SET_MISMATCH'):
        FundMemberGraphStore(None).normalize(plan,doc('fund-pcf','2026-10-23'),body())


@pytest.mark.parametrize('mutation',['url','index','key','day','fund'])
def test_source_policy_cannot_expand_the_bank_api_or_product_scope(mutation):
    from app.macro.official_member_policy import OfficialSource
    d=doc('fund-pcf','2026-10-23');OfficialSource.model_validate(d)
    if mutation=='url':d['url']+='?redirect=1'
    elif mutation=='index':d['indexCode']='000819'
    elif mutation=='key':d['subscriptionCode']='512801'
    elif mutation=='day':d['tradingDate']='20261023'
    else:d['fundCode']='512810'
    with pytest.raises(ValueError):OfficialSource.model_validate(d)


def test_bank_sample_and_new_statement_have_distinct_roots_without_changing_catalog(tmp_path):
    v=value();c=load(tmp_path,v);old=deepcopy(v)
    old['catalogEvidence'].update(discoveryEnabled=False,discoveryDocuments=[],memberGraphEnabled=False,memberGraphs=[])
    assert c.fingerprint==load(tmp_path,old).fingerprint


def test_bank_normal_discovery_same_day_graph_public_header_and_zero_http_reentry(database,tmp_path,monkeypatch):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    from app.macro.official_member_budget import DISCOVERY_JOB_PREFIX
    from app.db.models.ops import JobRun
    from sqlalchemy import select
    _,factory=database;c=load(tmp_path,value());discovery=import_module('app.macro.jobs.fund_contract_runtime');graph=import_module('app.macro.jobs.fund_member_graph_runtime')
    monkeypatch.setattr(discovery,'load_enterprise_target_catalog',lambda:c);monkeypatch.setattr(graph,'load_enterprise_target_catalog',lambda:c)
    calls=[]
    def handle(request):
        calls.append(str(request.url))
        if str(request.url)==METADATA:
            assert request.method=='GET'
            with factory() as session:assert session.scalar(select(JobRun).where(JobRun.job_name==DISCOVERY_JOB_PREFIX+'512800')).request_count==1
            return response(200,content=bank())
        if request.url.path.endswith('cons.xls'):
            return response(200,content=workbook([
                ['20261023','399986','中证银行','CSI Banks','000001','平安银行','PING AN','深圳证券交易所','Shenzhen Stock Exchange'],
                ['20261023','399986','中证银行','CSI Banks','600000','浦发银行','SPD BANK','上海证券交易所','Shanghai Stock Exchange']]))
        assert str(request.url)==PCF and request.method=='POST' and request.headers['netNo']=='web'
        payload=json.loads(request.content)
        assert payload['fundCode']=='512800' and payload['startDate']=='20261023' and payload['netNo']=='web'
        assert len(payload['signature'])==32 and payload['timestamp'].isdigit()
        return response(200,content=body())
    transport=httpx.MockTransport(handle)
    a=discovery.execute_fund_contracts(factory,c,clock=lambda:NOW,transport=transport)
    assert a['status']=='completed' and a['supplierHttp']==1,a
    b=graph.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(seconds=1),transport=transport)
    assert b['configuredGraphsCompleted'] and b['supplierHttp']==2,b
    witnesses=load_catalog_member_witnesses(factory,NOW+timedelta(seconds=2),c)
    assert len(witnesses)==1 and witnesses[0]['targetCode']=='512800.SH'
    proof=witnesses[0]
    assert proof['documents'][2]['requestParameters']=={'fundCode':'512800','startDate':'20261023'}
    from app.macro.enterprise_official_targets import valid_witness
    for mutation in ('day','requested-day','fund','extra'):
        bad=deepcopy(proof)
        if mutation=='day':bad['membershipDate']='2026-10-22'
        elif mutation=='requested-day':bad['documents'][2]['requestParameters']['startDate']='20261022'
        elif mutation=='fund':bad['documents'][2]['requestParameters']['fundCode']='512810'
        else:bad['documents'][2]['requestParameters']['extra']='unsafe'
        assert not valid_witness(bad,'512800.SH','etf',NOW.date())
    assert discovery.execute_fund_contracts(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert graph.execute_fund_graphs(factory,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert len(calls)==3
