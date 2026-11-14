from datetime import timedelta

import pytest

from app.macro.enterprise_official_targets import load_official_witnesses
from app.macro.official_member_store import OfficialMemberStore,build_plan
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_official_member_runtime import setup
from backend.tests.macro.test_official_member_store import NOW
from backend.tests.macro.official_xls_fixture import compound_workbook
from backend.tests.macro.test_enterprise_target_catalog import payload,load
from backend.tests.macro.test_official_member_policy import policy
from backend.tests.macro.test_official_fund_mapping import body as fund_body
from backend.tests.macro.test_official_pcf import body as pcf_body
from backend.tests.macro.test_official_members import row


def test_completed_visibility_uses_actual_receipt_time(database,tmp_path,monkeypatch):
    _,factory=database;_,catalog=setup(tmp_path,monkeypatch);s=OfficialMemberStore(factory)
    p=build_plan(catalog.official_policy,catalog.fingerprint,NOW);d=p['policy']['documents'][0]
    key=s.reserve(p,d,NOW);s.receive(key,compound_workbook(),NOW+timedelta(seconds=1));s.replay(key,NOW+timedelta(seconds=2))
    assert load_official_witnesses(factory,NOW+timedelta(seconds=1),catalog)==[]
    result=load_official_witnesses(factory,NOW+timedelta(seconds=3),catalog)
    assert len(result)==1 and result[0]['memberCount']==2 and result[0]['membershipDate']=='2026-10-23'


@pytest.mark.parametrize('same_date',[True,False])
def test_etf_requires_explicit_fund_identity_and_same_day_entire_csi_pcf_set(database,tmp_path,same_date):
    _,factory=database;value=payload();value['officialEvidence']=policy();value['officialEvidence']['collectionEnabled']=True
    if not same_date:
        value['officialEvidence']['documents'][3].update(tradingDate='2026-10-22',url='https://m.gtfund.com/cochin/etf/download/512881/20261022')
    catalog=load(tmp_path,value);p=build_plan(catalog.official_policy,catalog.fingerprint,NOW);s=OfficialMemberStore(factory)
    for i,d in enumerate(p['policy']['documents']):
        at=NOW+timedelta(seconds=i*3);key=s.reserve(p,d,at)
        if d['kind']=='csi-members':body=compound_workbook([row(index=d['indexCode'],code='600000'),row(index=d['indexCode'],code='000001',exchange='深圳证券交易所',exchangeEnglish='Shenzhen Stock Exchange')])
        elif d['kind']=='fund-mapping':body=fund_body()
        else:body=pcf_body(day='20261023' if same_date else '20261022')
        s.receive(key,body,at+timedelta(seconds=1));s.replay(key,at+timedelta(seconds=2))
    witnesses=load_official_witnesses(factory,NOW+timedelta(seconds=20),catalog)
    assert [w['targetCode'] for w in witnesses]==(['000300.SH','512880.SH'] if same_date else ['000300.SH'])
    if same_date:
        assert witnesses[1]['memberCount']==2 and len(witnesses[1]['documents'])==3
        assert witnesses[1]['membershipBasis']=='official-fund-statement-and-pcf-csi-same-date'
