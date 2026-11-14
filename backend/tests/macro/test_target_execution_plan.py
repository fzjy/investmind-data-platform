"""冻结日期、真实来源父链及有限回看；不接数据库/HTTP。"""
from copy import deepcopy
from datetime import date, datetime, UTC
from importlib import import_module

import pytest

from app.connectors.tushare.contracts import TushareResponse
from app.macro.target_evidence_contract import TargetEvidenceRequest
from app.macro.target_evidence_normalize import normalize_target_response
from app.stocks.universes.models import UniverseSelection
from backend.tests.macro.test_enterprise_target_catalog import payload, load


def implementation():
    try: return import_module('app.macro.target_execution_plan')
    except ImportError: pytest.fail('frozen target execution planner not implemented')


def configured(tmp_path, *, codes=('000300.SH','512880.SH'), size=2):
    value=payload(); value['evidencePolicy']['bootstrapTargetCodes']=list(codes)
    catalog=load(tmp_path,value)
    selection=UniverseSelection('custom','controlled','custom',tuple(f'{n:06}.SZ' for n in range(size)), 'b'*64)
    return catalog,selection


def result(root,step,catalog,items,observation='1'*64):
    value=step['request']
    req=TargetEvidenceRequest(value['targetCode'],value['targetKind'],value['apiName'],value['params'],tuple(value['fields']))
    response=TushareResponse(fields=req.fields,items=tuple(tuple(r) for r in items))
    normalized=normalize_target_response(req,response,datetime(2026,10,22,8,tzinfo=UTC),
        date.fromisoformat(root['snapshot']['frozenAsOfDate']),catalog.evidence_policy,'Asia/Shanghai')
    return {'observationId':observation,'payload':normalized}


@pytest.mark.parametrize(('as_of','windows'),[
    (date(2026,1,8),[['20260101','20260108'],['20251201','20251231'],['20251101','20251130']]),
    (date(2024,3,1),[['20240301','20240301'],['20240201','20240229'],['20240101','20240131']]),
    (date(2026,10,22),[['20261001','20261022'],['20260901','20260930'],['20260801','20260831']]),
])
def test_month_windows_never_extend_past_frozen_day(tmp_path,as_of,windows):
    mod=implementation(); catalog,selection=configured(tmp_path,size=61)
    root=mod.freeze_target_plan(catalog,selection,as_of,'Asia/Shanghai')
    assert root['snapshot']['windows']==windows
    assert root['snapshot']['pool']['size']==61
    assert len(root['snapshot']['pool']['symbols'])==61
    assert mod.validate_target_plan(root,catalog,selection,'Asia/Shanghai',date(2026,10,23))==root


@pytest.mark.parametrize('change',['catalog','pool','timezone','root-hash','future'])
def test_frozen_identity_drift_or_tampering_is_not_new_budget(tmp_path,change):
    mod=implementation(); catalog,selection=configured(tmp_path)
    root=mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai'); before=deepcopy(root)
    zone='Asia/Shanghai'; today=date(2026,10,23)
    if change=='catalog':
        value=payload(); value['catalogVersion']='changed'; catalog=load(tmp_path,value)
    if change=='pool': _,selection=configured(tmp_path,size=61)
    if change=='timezone': zone='UTC'
    if change=='root-hash': root['snapshot']['windows'][0][1]='20261023'
    if change=='future': today=date(2026,10,21)
    with pytest.raises(ValueError,match='TARGET_'): mod.validate_target_plan(root,catalog,selection,zone,today)
    if change!='root-hash': assert root==before


def test_empty_or_duplicate_pool_is_not_a_valid_frozen_scope(tmp_path):
    mod=implementation(); catalog,selection=configured(tmp_path,size=0)
    with pytest.raises(ValueError,match='TARGET_'): mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai')
    from dataclasses import replace
    selection=replace(selection,symbols=('000001.SZ','000001.SZ'))
    with pytest.raises(ValueError,match='TARGET_'): mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai')


def test_index_metadata_then_first_nonempty_month_finishes_without_older_fetch(tmp_path):
    mod=implementation(); catalog,selection=configured(tmp_path,codes=('000300.SH',))
    root=mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai')
    step=mod.derive_target_steps(root,{})['next']
    assert step['request']['params']=={'ts_code':'000300.SH','market':'CSI'} and step['parentObservationIds']==[]
    outputs={step['requestKey']:result(root,step,catalog,[['000300.SH','沪深300','沪深300指数','CSI','中证',None]])}
    weight=mod.derive_target_steps(root,outputs)['next']
    assert weight['request']['params']=={'index_code':'000300.SH','start_date':'20261001','end_date':'20261022'}
    assert weight['parentObservationIds']==['1'*64]
    outputs[weight['requestKey']]=result(root,weight,catalog,[['000300.SH','600519.SH','20261020',0]],'2'*64)
    progress=mod.derive_target_steps(root,outputs)
    assert progress['next'] is None and progress['processingCompleted'] is True
    assert progress['observedTargetCount']==1 and progress['terminalGaps']==[]


def test_etf_uses_exact_persisted_mapping_and_two_parent_observations(tmp_path):
    mod=implementation(); catalog,selection=configured(tmp_path,codes=('512880.SH',))
    root=mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai')
    etf=mod.derive_target_steps(root,{})['next']
    assert etf['request']['apiName']=='etf_basic'
    outputs={etf['requestKey']:result(root,etf,catalog,[['512880.SH','证券ETF','399975.SZ','证券公司','SH','L','境内']],'3'*64)}
    index=mod.derive_target_steps(root,outputs)['next']
    assert index['request']['params']=={'ts_code':'399975.SZ','market':'CSI'}
    assert index['parentObservationIds']==['3'*64]
    outputs[index['requestKey']]=result(root,index,catalog,[['399975.SZ','证券','证券公司指数','CSI','中证',None]],'4'*64)
    weight=mod.derive_target_steps(root,outputs)['next']
    assert weight['request']['params']['index_code']=='399975.SZ'
    assert weight['parentObservationIds']==['3'*64,'4'*64]
    assert weight['request']['targetCode']=='512880.SH'


def test_empty_months_stop_at_configured_window_without_claiming_completeness(tmp_path):
    mod=implementation(); catalog,selection=configured(tmp_path,codes=('000300.SH',))
    root=mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai')
    step=mod.derive_target_steps(root,{})['next']; outputs={step['requestKey']:result(root,step,catalog,[['000300.SH','300','300','CSI','中证',None]])}
    for month,obs in [('20261001','2'*64),('20260901','3'*64),('20260801','4'*64)]:
        step=mod.derive_target_steps(root,outputs)['next']; assert step['request']['params']['start_date']==month
        outputs[step['requestKey']]=result(root,step,catalog,[],obs)
    progress=mod.derive_target_steps(root,outputs)
    assert progress['next'] is None and progress['processingCompleted'] is True and progress['observedTargetCount']==0
    assert progress['terminalGaps']==[{'targetCode':'000300.SH','reason':'NO_MEMBERS'}]


def test_bad_metadata_or_unreachable_outcome_never_opens_future_stage(tmp_path):
    mod=implementation(); catalog,selection=configured(tmp_path,codes=('000300.SH',))
    root=mod.freeze_target_plan(catalog,selection,date(2026,10,22),'Asia/Shanghai')
    step=mod.derive_target_steps(root,{})['next']; outputs={step['requestKey']:result(root,step,catalog,[])}
    progress=mod.derive_target_steps(root,outputs)
    assert progress['next'] is None and progress['terminalGaps']==[{'targetCode':'000300.SH','reason':'NO_METADATA'}]
    outputs['9'*64]=outputs[step['requestKey']]
    with pytest.raises(ValueError,match='TARGET_'): mod.derive_target_steps(root,outputs)
