from copy import deepcopy
from datetime import date
import importlib
import importlib.util

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.connectors.tushare.contracts import TushareResponse
from app.db.base import Base
from app.db.session import create_engine_and_session
from backend.tests.macro.test_rea_input_preview import data, preview


def implementation():
    assert importlib.util.find_spec('app.macro.rea_input_store'), 'REA durable store not implemented'
    return importlib.import_module('app.macro.rea_input_store')


@pytest.fixture
def database(tmp_path):
    module=implementation()
    models=importlib.import_module('app.db.models.enterprise_rea_inputs')
    url=f'sqlite:///{(tmp_path/"rea.sqlite").as_posix()}'
    engine,factory=create_engine_and_session(url)
    Base.metadata.create_all(engine)
    yield module,models,url,engine,factory
    engine.dispose()


def response_for(request,source,changes=None):
    values={**source.values,'ts_code':source.symbol,'ann_date':source.ann_date.strftime('%Y%m%d') if source.ann_date else None,
        'f_ann_date':source.f_ann_date.strftime('%Y%m%d') if source.f_ann_date else None,'end_date':source.end_date.strftime('%Y%m%d'),
        'report_type':source.report_type,'comp_type':source.comp_type,'update_flag':source.update_flag,
        **{field:9 for field in request['missingFields']},**(changes or {})}
    return TushareResponse(fields=request['fields'],items=[[values[f] for f in request['fields']]])


def frozen(database):
    module,models,url,engine,factory=database
    inputs=data(1);plan=preview(inputs);store=module.ReaInputStore(factory)
    store.freeze(plan,inputs,5000)
    request=plan['requests'][0]
    source=next(r for r in inputs.financials if r.source_key==request['coreBinding']['sourceKey'])
    return store,plan,inputs,request,source


def test_freeze_and_raw_replay_survive_engine_restart_without_overwriting_results(database):
    store,plan,inputs,request,source=frozen(database)
    module,models,url,engine,factory=database
    assert store.latest()['plan']==plan
    assert store.latest()['sources'][source.source_key]==source
    store.reserve(request,plan['planFingerprint'],'run-1',5000)
    store.receive(request['requestKey'],response_for(request,source))
    assert store.recover(request,source,5000)['status']=='matched'
    assert not store.result_exists(request['requestKey'])
    engine.dispose();new_engine,new_factory=create_engine_and_session(url)
    try:
        resumed=module.ReaInputStore(new_factory)
        value=resumed.recover(request,source,5000,persist=True)
        assert set(value['fields'])==set(request['missingFields'])
        assert resumed.result_exists(request['requestKey'])
        assert resumed.recover(request,source,5000,persist=True)==value
        with new_factory() as session:
            assert len(list(session.scalars(select(models.ReaInputPlan))))==1
            assert len(list(session.scalars(select(models.ReaInputSupplement))))==1
    finally: new_engine.dispose()


def test_each_target_has_one_durable_reservation_and_unknown_outcome_cannot_be_reissued(database):
    store,plan,_,request,source=frozen(database)
    store.reserve(request,plan['planFingerprint'],'run-1',5000)
    with pytest.raises(ValueError,match='REA_ATTEMPT_UNCERTAIN'): store.recover(request,source,5000)
    with pytest.raises(IntegrityError): store.reserve(request,plan['planFingerprint'],'run-2',5000)
    store.fail(request['requestKey'],'TushareNetworkError')
    with pytest.raises(ValueError,match='REA_ATTEMPT_FAILED'): store.recover(request,source,5000)


@pytest.mark.parametrize('damage',['raw','result','plan','source','page'])
def test_corrupted_or_changed_evidence_is_not_silently_reused(database,damage):
    store,plan,_,request,source=frozen(database)
    _,models,_,_,factory=database
    store.reserve(request,plan['planFingerprint'],'run-1',5000)
    store.receive(request['requestKey'],response_for(request,source))
    store.recover(request,source,5000,persist=True)
    with factory.begin() as session:
        if damage=='raw': session.get(models.ReaInputResponse,request['requestKey']).response_hash='0'*64
        elif damage=='result': session.get(models.ReaInputSupplement,request['requestKey']).outcome={'status':'matched','fields':{}}
        elif damage=='plan': session.get(models.ReaInputPlan,plan['planFingerprint']).snapshot_hash='0'*64
    if damage=='source':
        from dataclasses import replace
        source=replace(source,values={**source.values,'money_cap':123})
    with pytest.raises(ValueError):
        store.latest() if damage=='plan' else store.recover(request,source,4000 if damage=='page' else 5000)


def test_field_null_is_terminal_evidence_and_not_zero(database):
    store,plan,_,request,source=frozen(database)
    store.reserve(request,plan['planFingerprint'],'run-1',5000)
    store.receive(request['requestKey'],response_for(request,source,{'fix_assets':None}))
    result=store.recover(request,source,5000,persist=True)
    assert result['fields']['fix_assets']=={'status':'returned-null','value':None}
    assert result['fields']['acct_payable']=={'status':'matched','value':'9'}


def test_received_attempt_and_snapshot_identity_are_verified(database):
    store,plan,_,request,source=frozen(database)
    _,models,_,_,factory=database
    with pytest.raises(ValueError): store.receive('unknown',response_for(request,source))
    with pytest.raises(ValueError): store.fail('unknown','TushareNetworkError')
    changed=deepcopy(plan);changed['requests'][0]['apiName']='arbitrary'
    with pytest.raises(ValueError): store.freeze(changed,data(1),5000)
    store.reserve(request,plan['planFingerprint'],'run-1',5000)
    store.receive(request['requestKey'],response_for(request,source))
    with factory.begin() as session: session.get(models.ReaInputAttempt,request['requestKey']).state='reserved'
    with pytest.raises(ValueError): store.recover(request,source,5000)
