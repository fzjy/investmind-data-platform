from copy import deepcopy
from datetime import date
from dataclasses import replace
from importlib import import_module

import pytest

from app.macro.enterprise import EnterprisePolicy,MarketValueInput
from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
from backend.tests.macro.test_enterprise_rea import controlled
from backend.tests.macro.test_official_member_runtime import setup

AS_OF=date(2026,10,25)
POLICY=EnterprisePolicy(min_samples=1)


def witness(members,code='000300.SH',kind='index'):
    index='000300' if kind=='index' else '399975'
    docs=[{'kind':'csi-members','url':f'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{index}cons.xls',
        'rawSha256':'a'*64,'runId':'1c37218d-8a5d-51cc-8e93-e18bf999e36b',
        'retrievedAt':'2026-10-25T04:00:00+00:00','completedAt':'2026-10-25T04:00:01+00:00'}]
    if kind=='etf':
        docs += [{'kind':'fund-mapping','url':'https://e.gtfund.com/etrade/Jijin/view/id/512880#etf-list','rawSha256':'b'*64,
            'runId':'92b240ac-dd8c-5000-944d-f0f67d1981b5','retrievedAt':'2026-10-25T04:00:00+00:00','completedAt':'2026-10-25T04:00:01+00:00'},
            {'kind':'fund-pcf','url':'https://m.gtfund.com/cochin/etf/download/512881/20261023','rawSha256':'c'*64,
            'runId':'c4035909-a978-58e2-9b80-ad69593d4db4','retrievedAt':'2026-10-25T04:00:00+00:00','completedAt':'2026-10-25T04:00:01+00:00'}]
    return {'targetCode':code,'kind':kind,'indexCode':index,'membershipDate':'2026-10-23',
        'members':sorted(members),'memberCount':len(members),'planFingerprint':'d'*64,
        'catalogFingerprint':'e'*64,'membershipBasis':'official-csi-complete-file' if kind=='index' else 'official-fund-statement-and-pcf-csi-same-date',
        'trackingIndexName':None if kind=='index' else '中证全指证券公司指数','documents':docs}


def build(tmp_path,monkeypatch,data=None,witnesses=None):
    _,catalog=setup(tmp_path,monkeypatch,two=False)
    data=data or controlled(61);parent=public_enterprise_analysis(data,AS_OF,POLICY,rea_enabled=True)
    proofs=witnesses if witnesses is not None else [witness([*data.symbols[:27],'999999.SH'])]
    for proof in proofs:proof['catalogFingerprint']=catalog.fingerprint
    module=import_module('app.macro.enterprise_official_targets')
    result=module.build_official_target_comparison(data,AS_OF,POLICY,parent,catalog,proofs)
    return module,result,parent,data


def test_actual_pool_intersection_is_dynamic_and_does_not_change_parent(tmp_path,monkeypatch):
    module,result,parent,data=build(tmp_path,monkeypatch)
    target=result['targets'][0];child=target['analysis']
    assert parent['poolCount']==61 and child['poolCount']==27
    assert target['witness']['memberCount']==28 and child['symbols']==list(data.symbols[:27])
    assert child['policy']==parent['policy'] and 'officialTargets' not in child
    assert module.valid_official_target_comparison(result,parent)
    assert {q['candidateCount'] for m in child['rea']['series'] for q in m['quality']}<={0,27}


def test_all_financial_evidence_and_valuation_inputs_are_scoped(tmp_path,monkeypatch):
    from app.macro.enterprise import ReaFieldInput,ReceivableInput
    data=controlled(3);keys={r.symbol:r.source_key for r in data.financials}
    data=replace(data,market_values=tuple(MarketValueInput(s,date(2024,7,1),100,'mv:'+s) for s in data.symbols),
        receivables=tuple(ReceivableInput(keys[s],'a'*64,'ready','1','b'*64,date(2024,1,1)) for s in data.symbols),
        contract_liabilities=tuple(ReceivableInput(keys[s],'a'*64,'ready','2','b'*64,date(2024,1,1)) for s in data.symbols),
        rea_fields=tuple(ReaFieldInput(keys[s],'a'*64,'inventory','ready','1','b'*64,date(2024,1,1),'request:'+s) for s in data.symbols))
    module=import_module('app.macro.enterprise_industries')
    child=module.subset_enterprise_inputs(data,[data.symbols[1]],['2024-06-30'])
    assert child.symbols==(data.symbols[1],)
    for field in ('financials','securities','industries','market_values'):assert all(r.symbol==data.symbols[1] for r in getattr(child,field))
    for field in ('receivables','contract_liabilities','rea_fields'):assert {r.source_key for r in getattr(child,field)}=={keys[data.symbols[1]]}
    assert child.display_periods==(date(2024,6,30),)


def test_no_intersection_and_missing_proof_are_explicit(tmp_path,monkeypatch):
    module,result,parent,_=build(tmp_path,monkeypatch,witnesses=[witness(['999999.SH'])])
    target=result['targets'][0]
    assert target['status']=='no-intersection' and target['analysis'] is None and target['poolMembers']==[]
    assert module.valid_official_target_comparison(result,parent)
    _,empty,_,_=build(tmp_path,monkeypatch,witnesses=[])
    assert empty['targets'][0]['status']=='evidence-missing' and empty['targets'][0]['witness'] is None


def test_tampered_scope_count_dates_and_nested_analysis_rejected(tmp_path,monkeypatch):
    module,result,parent,_=build(tmp_path,monkeypatch)
    for mutate in [lambda r:r['targets'][0]['witness'].update(memberCount=300),
        lambda r:r['targets'][0]['witness'].update(membershipDate='2026-02-30'),
        lambda r:r['targets'][0]['poolMembers'].append('999999.SH'),
        lambda r:r['targets'][0]['analysis'].update(officialTargets={}),
        lambda r:r['targets'][0]['analysis']['policy'].update(min_samples=0)]:
        bad=deepcopy(result);mutate(bad)
        assert not module.valid_official_target_comparison(bad,parent)


def test_root_contract_rejects_invalid_optional_official_analysis(tmp_path,monkeypatch):
    _,result,parent,_=build(tmp_path,monkeypatch)
    parent['officialTargets']=result
    assert valid_enterprise_analysis(parent)
    bad=deepcopy(parent);bad['officialTargets']['targets'][0]['poolMembers'].append('999999.SH')
    assert not valid_enterprise_analysis(bad)


def test_future_witness_cannot_be_used_by_builder_or_contract(tmp_path,monkeypatch):
    module,result,parent,data=build(tmp_path,monkeypatch)
    bad=deepcopy(result)
    for d in bad['targets'][0]['witness']['documents']:
        d.update(retrievedAt='2099-01-01T00:00:00+00:00',completedAt='2099-01-01T00:00:01+00:00')
    assert not module.valid_official_target_comparison(bad,parent)
    _,catalog=setup(tmp_path,monkeypatch)
    with pytest.raises(ValueError):
        module.build_official_target_comparison(data,AS_OF,POLICY,parent,catalog,[bad['targets'][0]['witness']])


def test_witness_cutoff_is_end_of_analysis_date_in_shanghai(tmp_path,monkeypatch):
    module,result,parent,_=build(tmp_path,monkeypatch)
    proof=result['targets'][0]['witness']
    for d in proof['documents']:
        d.update(retrievedAt='2026-10-25T15:59:59.999999+00:00',completedAt='2026-10-25T15:59:59.999999+00:00')
    assert module.valid_official_target_comparison(result,parent)
    proof['documents'][0]['completedAt']='2026-10-25T16:00:00+00:00'
    assert not module.valid_official_target_comparison(result,parent)
