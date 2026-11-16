from copy import deepcopy
from datetime import date
import importlib
import importlib.util

import pytest

from app.macro.jobs.macro_publish import MacroPublishJob
from app.marts.dto import validate_product_payload
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.macro.marts.macro_quality import evaluate_macro_quality
from backend.tests.jobs.test_macro_publish import setup, AS_OF, CATALOG
from backend.tests.macro.test_enterprise import inputs
from app.macro.enterprise import EnterprisePolicy, build_enterprise_analysis


def contract_module():
    if importlib.util.find_spec('app.macro.enterprise_contract') is None:
        pytest.fail('enterprise publication contract not implemented')
    return importlib.import_module('app.macro.enterprise_contract')


def artifact():
    return contract_module().public_enterprise_analysis(inputs(), date(2024,5,31), EnterprisePolicy(min_samples=1))


@pytest.mark.parametrize('damage', [None, 'missing-card', 'unknown-version', 'unit', 'arithmetic', 'coverage'])
def test_v2_white_strip_contract_accepted_but_damaged_third_card_rejected(damage):
    from backend.tests.macro.test_enterprise_white_strip import enabled
    value = contract_module().public_enterprise_analysis(enabled(), date(2024,5,31), EnterprisePolicy(min_samples=1))
    c = value['cards'][-1]
    assert c['id'] == 'white-strip'
    if damage == 'missing-card': value['cards'].pop()
    if damage == 'unknown-version': value['algorithmVersion'] = 'enterprise-quarter-v3'
    if damage == 'unit': c['resultUnit'] = '%'
    if damage == 'arithmetic': c['points'][-1]['result'] = 999
    if damage == 'coverage': c['quality'][-1]['coverage'] = 99
    assert contract_module().valid_enterprise_analysis(value) is (damage is None)


def test_public_contract_keeps_decisions_and_hashes_full_provenance_without_massive_refs():
    result=artifact()
    full=build_enterprise_analysis(inputs(),date(2024,5,31),EnterprisePolicy(min_samples=1))
    assert result['symbols']==list(inputs().symbols)
    assert result['inputHash']==full['inputHash']
    q=result['cards'][0]['quality'][-1]
    assert q['included']==['000001.SZ','000002.SZ']
    assert 'sourceRefs' not in q and len(q['provenanceHash'])==64 and q['sourceRefCount']>0
    assert contract_module().valid_enterprise_analysis(result)


@pytest.mark.parametrize('damage', ['schema','unit','bool','arithmetic','coverage','membership','duplicate','quality','hash','policy'])
def test_public_contract_rejects_malformed_or_inconsistent_payload(damage):
    result=artifact(); c=result['cards'][0]; p=c['points'][-1]; q=c['quality'][-1]
    if damage=='schema': result['schemaVersion']='unknown'
    if damage=='unit': c['resultUnit']='%'
    if damage=='bool': p['primary']=True
    if damage=='arithmetic': p['result']=500
    if damage=='coverage': q['coverage']=99
    if damage=='membership': q['included'][0]='outside'
    if damage=='duplicate': c['points'].append(deepcopy(p))
    if damage=='quality': q['status']='insufficient-samples'
    if damage=='hash': result['inputHash']='x'
    if damage=='policy': result['policy']['min_samples']=True
    assert not contract_module().valid_enterprise_analysis(result)


def test_macro_pair_captures_enterprise_once_reuses_on_reentry_and_blocks_forgery_atomically(pg_factory):
    engine,factory,_,history,publisher,statuses=setup(pg_factory, 20)
    captures=[]
    def loader(as_of):
        captures.append(as_of)
        return contract_module().public_enterprise_analysis(inputs(),as_of.date(),EnterprisePolicy(min_samples=1))
    try:
        builder=MacroMartBuilder(MacroRepository(factory),catalog=CATALOG,enterprise_loader=loader)
        job=MacroPublishJob(builder,publisher,history)
        first=job.publish('macro-run',AS_OF,CATALOG,statuses,reuse_unchanged=True)
        assert first.status=='completed' and len(captures)==1
        assert publisher.current('macro-transmission').payload['enterpriseAnalysis']['inputHash']
        again=job.publish('macro-run',AS_OF,CATALOG,statuses,reuse_unchanged=True)
        assert again.version==first.version and again.promoted_products==() and len(captures)==2
        original_build=builder.build
        def forged(*args,**kwargs):
            candidates=original_build(*args,**kwargs)
            candidates[0].payload['enterpriseAnalysis']['poolVersion']='forged'
            return candidates
        builder.build=forged
        report=job.publish('macro-run',AS_OF,CATALOG,statuses)
        assert report.blocked and publisher.current('macro-transmission').version==first.version
        assert publisher.current('macro-insights').version==first.version
        assert not next(q for q in report.quality if q.name=='macro-enterprise-source-bound').passed
    finally:
        engine.dispose()


def test_old_payload_readable_but_new_enterprise_payload_cannot_publish_without_trusted_source(pg_factory):
    engine,factory,_,_,_,statuses=setup(pg_factory, 20)
    try:
        builder=MacroMartBuilder(MacroRepository(factory),catalog=CATALOG)
        observations=builder.load_observations(CATALOG,AS_OF)
        pair=builder.build(AS_OF,'pending',observations=observations)
        assert not validate_product_payload('macro-transmission',pair[0].payload)
        pair[0].payload['enterpriseAnalysis']=artifact()
        assert not validate_product_payload('macro-transmission',pair[0].payload)
        checks=evaluate_macro_quality(CATALOG,pair,statuses,observations=observations)
        assert not next(q for q in checks if q.name=='macro-enterprise-source-bound').passed
        pair[0].payload['enterpriseAnalysis']['poolCount']=True
        assert validate_product_payload('macro-transmission',pair[0].payload)
    finally:
        engine.dispose()


def test_configured_loader_returning_none_cannot_silently_clear_cards(pg_factory):
    engine,factory,_,history,publisher,statuses=setup(pg_factory, 20)
    try:
        builder=MacroMartBuilder(MacroRepository(factory),catalog=CATALOG,enterprise_loader=lambda _:None)
        result=MacroPublishJob(builder,publisher,history).publish('macro-run',AS_OF,CATALOG,statuses)
        assert result.blocked and publisher.current('macro-transmission') is None
    finally:
        engine.dispose()


def test_full_product_build_keeps_trusted_enterprise_capture_and_rejects_changed_candidate(pg_factory, monkeypatch):
    from app.config import Settings
    from app.jobs.build_and_publish import DefaultMartBuilders, BuildAndPublishJob, BuildAndPublishOptions
    from app.jobs.build_history import SqlBuildHistory
    from app.stocks.derived.inputs import CoreSnapshotRepository
    from app.stocks.derived.pipeline import DerivedPipeline
    from app.marts.quality import QualityGate
    from backend.tests.jobs.test_build_and_publish_integration import _seed_complete_core_sample
    from backend.tests.marts.test_macro_derivations import row
    from app.marts.publication import SnapshotPublisher
    factory=pg_factory; _seed_complete_core_sample(factory)
    with factory.begin() as s:
        s.add_all(row(item.id,'2026-08-01',1) for item in CATALOG.series)
    calls=[]
    def loader(as_of):
        calls.append(as_of)
        return contract_module().public_enterprise_analysis(inputs(),as_of,EnterprisePolicy(min_samples=1))
    monkeypatch.setattr(contract_module(),'enterprise_loader',lambda factory,settings:loader)
    builders=DefaultMartBuilders(factory,settings=Settings())
    publisher=SnapshotPublisher(factory)
    job=BuildAndPublishJob(CoreSnapshotRepository(factory),DerivedPipeline(factory),builders,QualityGate(),publisher,SqlBuildHistory(factory))
    options=BuildAndPublishOptions(AS_OF.date(),('600519.SH',))
    first=job.run(options)
    assert first.status=='completed' and len(calls)==1
    assert publisher.current('macro-transmission').payload['enterpriseAnalysis']['poolVersion']=='test-pool'
    before={p.product:p.version for p in publisher.current_macro_pair()}
    original=builders._macro.build
    def forge(*args,**kwargs):
        pair=original(*args,**kwargs)
        pair[0].payload['enterpriseAnalysis']['poolVersion']='forged'
        return pair
    monkeypatch.setattr(builders._macro,'build',forge)
    bad=job.run(options)
    assert not next(q for q in bad.quality if q.name=='macro-enterprise-source-bound').passed
    assert {p.product:p.version for p in publisher.current_macro_pair()}==before


@pytest.mark.parametrize('universe',['custom','all'])
def test_other_stock_universe_modes_do_not_expand_or_disable_fixed_enterprise_pool(pg_factory, universe):
    from app.config import Settings
    engine,factory,_,_,_,_=setup(pg_factory, 20)
    try:
        # 有完整空Core表；生产loader必须仍只读固定50股，不能因股票同步模式不同整体失败。
        load=contract_module().enterprise_loader(factory,Settings(sync_universe=universe))
        result=load(AS_OF)
        assert result['poolCount']==len(result['symbols'])==50
        assert {'000001.SZ','600519.SH','601398.SH'} <= set(result['symbols'])
        assert all(c['points']==[] for c in result['cards'])
    finally:
        engine.dispose()
