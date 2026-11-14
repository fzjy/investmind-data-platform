from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from importlib import import_module
import json

import httpx
import pytest

from app.macro.official_member_policy import OfficialSource
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_issuer_contract_samples import value as base_value,NOW
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_official_pcf import body as pcf_body
from backend.tests.macro.official_xls_fixture import workbook

SPECS={'512400':('000819','中证申万有色金属指数','ETFYSJS','南方中证申万有色金属交易型开放式指数证券投资基金'),
    '512200':('931775','中证全指房地产指数','ETFFDC','南方中证全指房地产交易型开放式指数证券投资基金')}


def doc(code):return {'id':'fund-'+code,'kind':'fund-mapping','indexCode':SPECS[code][0],'fundCode':code,
    'subscriptionCode':None,'tradingDate':None,'url':'https://www.nffund.com/nfwebApi/fund/overreview'}


def value():
    v=base_value();p=v['catalogEvidence'];p.update(collectionEnabled=True,discoveryEnabled=True,memberGraphEnabled=True)
    v['officialEvidence']['dailyBudgetEnabled']=False
    for code,(_,name,_,_) in SPECS.items():
        v['targets'].append({'kind':'etf','code':code+'.SH','label':'测试ETF','sourceIndexNameHint':name})
        p['targets'].append({'code':code+'.SH','documents':[]})
        p.setdefault('discoveryDocuments',[]).append(doc(code))
        p.setdefault('memberGraphs',[]).append({'code':code+'.SH','discoveryDocumentId':'fund-'+code})
        p['sampleDocuments'].append({'fundCode':code,'mediaType':'html','url':f'https://www.nffund.com/new/personal-financing/detail.html?fundCode={code}'})
    return v


def body(code='512400'):
    _,name,key,full=SPECS[code]
    return json.dumps({'code':'ETS-5BP00000','message':'操作成功','data':{'fund_info':{
        'fundCode':code,'fundNameEx':full,'jjglr':'南方基金管理股份有限公司','etfCode':key,
        'yjbjjz':'本基金标的指数为'+name,'tzfw':'本基金主要投资于标的指数成份股。'}}},ensure_ascii=False).encode()


@pytest.mark.parametrize('code',list(SPECS))
def test_real_nffund_contract_binds_product_declaration_and_resource_not_other_funds(code):
    api=import_module('app.macro.fund_contract_discovery');raw=body(code);v=api.parse_fund_contract(raw,code,SPECS[code][1])
    assert v=={'schemaVersion':'1.0','fundCode':code,'fundName':SPECS[code][3],'trackingIndexName':SPECS[code][1],
        'downloadResourceKey':SPECS[code][2],'rawSha256':sha256(raw).hexdigest()}
    assert OfficialSource.model_validate(doc(code)).fundCode==code


@pytest.mark.parametrize('mutation',['wrong-fund','wrong-name','wrong-manager','benchmark-only','wrong-key','wrong-status','duplicate-json'])
def test_metadata_contract_rejects_ambiguous_or_unrelated_response(mutation):
    raw=body();v=json.loads(raw);info=v['data']['fund_info']
    if mutation=='wrong-fund':info['fundCode']='512200'
    elif mutation=='wrong-name':info['fundNameEx']=SPECS['512200'][3]
    elif mutation=='wrong-manager':info['jjglr']='其他管理人'
    elif mutation=='benchmark-only':info['yjbjjz']='比较基准为中证申万有色金属指数'
    elif mutation=='wrong-key':info['etfCode']='ETFFDC'
    elif mutation=='wrong-status':v['code']='ERROR'
    raw=json.dumps(v,ensure_ascii=False).encode()
    if mutation=='duplicate-json':raw=raw.replace(b'"fundCode":',b'"fundCode":"512200","fundCode":')
    with pytest.raises(ValueError):import_module('app.macro.fund_contract_discovery').parse_fund_contract(raw,'512400',SPECS['512400'][1])


@pytest.mark.parametrize('mutation',['url','index','key','date','cross-fund'])
def test_pcf_source_is_closed_to_registered_fund_resource_index_and_real_date(mutation):
    d={**doc('512400'),'id':'pcf-512400','kind':'fund-pcf','subscriptionCode':'ETFYSJS','tradingDate':'2026-10-28',
        'url':'https://www.nffund.com/etf/bulletin/ETFYSJS/ssepcf_512400_20261028.xml'}
    OfficialSource.model_validate(d)
    if mutation=='url':d['url']+='?redirect=1'
    elif mutation=='index':d['indexCode']='931775'
    elif mutation=='key':d['subscriptionCode']='ETFFDC'
    elif mutation=='date':d['tradingDate']='2026-10-27'
    else:d['fundCode']='512200'
    with pytest.raises(ValueError):OfficialSource.model_validate(d)


def test_sample_history_can_coexist_with_closed_current_contract_without_catalog_reidentity(tmp_path):
    v=value();c=load(tmp_path,v);old=deepcopy(v)
    old['catalogEvidence']['discoveryDocuments']=[];old['catalogEvidence']['discoveryEnabled']=False
    old['catalogEvidence']['memberGraphs']=[];old['catalogEvidence']['memberGraphEnabled']=False
    assert c.fingerprint==load(tmp_path,old).fingerprint


@pytest.mark.parametrize('code',list(SPECS))
def test_normal_discovery_and_same_day_graph_receive_raw_then_reenter_zero_http(database,tmp_path,monkeypatch,code):
    from app.macro.catalog_member_view import load_catalog_member_witnesses
    from app.db.models.ops import JobRun
    from sqlalchemy import select
    _,f=database;v=value();p=v['catalogEvidence']
    p['discoveryDocuments']=[doc(code)];p['memberGraphs']=[{'code':code+'.SH','discoveryDocumentId':'fund-'+code}]
    c=load(tmp_path,v);discovery=import_module('app.macro.jobs.fund_contract_runtime');graph=import_module('app.macro.jobs.fund_member_graph_runtime')
    monkeypatch.setattr(discovery,'load_enterprise_target_catalog',lambda:c);monkeypatch.setattr(graph,'load_enterprise_target_catalog',lambda:c)
    calls=[];index=SPECS[code][0]
    def handle(request):
        calls.append(str(request.url))
        if request.method=='POST':
            assert request.content==('fundCode='+code).encode()
            with f() as session:assert session.scalar(select(JobRun).where(JobRun.business_key=='fund-contract:'+code)).request_count==1
            return response(200,content=body(code))
        if request.url.path.endswith('cons.xls'):
            rows=[['20261023',index,SPECS[code][1],'INDEX','000001','平安银行','PING AN','深圳证券交易所','Shenzhen Stock Exchange'],
                ['20261023',index,SPECS[code][1],'INDEX','600000','浦发银行','SPD BANK','上海证券交易所','Shanghai Stock Exchange']]
            return response(200,content=workbook(rows))
        assert str(request.url)==f'https://www.nffund.com/etf/bulletin/{SPECS[code][2]}/ssepcf_{code}_20261023.xml'
        return response(200,content=pcf_body(code=code,day='20261023'))
    transport=httpx.MockTransport(handle)
    a=discovery.execute_fund_contracts(f,c,clock=lambda:NOW,transport=transport)
    assert a['status']=='completed' and a['supplierHttp']==1,a
    b=graph.execute_fund_graphs(f,c,clock=lambda:NOW+timedelta(seconds=1),transport=transport)
    assert b['configuredGraphsCompleted'] and b['supplierHttp']==2,b
    witnesses=load_catalog_member_witnesses(f,NOW+timedelta(seconds=2),c)
    assert len(witnesses)==1 and witnesses[0]['targetCode']==code+'.SH' and witnesses[0]['members']==['000001.SZ','600000.SH']
    assert discovery.execute_fund_contracts(f,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert graph.execute_fund_graphs(f,c,clock=lambda:NOW+timedelta(days=1),transport=transport)['supplierHttp']==0
    assert len(calls)==3
