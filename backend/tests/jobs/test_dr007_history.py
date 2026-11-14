"""真实 SQLite raw/core 与游标；仅供应商调用受控。"""
from datetime import UTC, date, datetime
from importlib import import_module, util

import pytest
from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.errors import TusharePermissionError
from app.db.base import Base, ExternalBase, MartBase
from app.db.models import JobRun, MacroObservation, MacroSeriesState, PublishedSnapshot, SourcePayload
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition
from backend.tests.connectors.macro.test_tushare_dr007 import SOURCE

NOW=datetime(2026,10,8,15,tzinfo=UTC)


class Client:
    def __init__(self, *, empty=False, fail=False):
        self.calls=[]
        self.empty, self.fail=empty,fail
    def call(self, api, params, fields):
        assert api == 'repo_daily' and fields == ('trade_date','ts_code','repo_maturity','weight')
        self.calls.append(dict(params))
        if self.fail:
            raise TusharePermissionError('sensitive controlled-token')
        return TushareResponse(fields=fields,items=() if self.empty else (
            (params['start_date'], 'DR007.IB','DR007',1.3665),))


def test_dr007_monthly_queue_is_available():
    assert util.find_spec('app.macro.jobs.dr007_history') is not None, 'DR007 month queue is missing'


@pytest.fixture
def setup():
    if util.find_spec('app.macro.jobs.dr007_history') is None:
        pytest.skip('entry RED covers missing queue')
    module=import_module('app.macro.jobs.dr007_history')
    repository=import_module('app.macro.jobs.dr007_history_repository')
    engine,factory=create_engine_and_session('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    cursors=SqlJobCursorRepository(factory,job_name='backfill-dr007-history')
    lock=ProcessLocalJobLock()
    def job(client, *,clock=lambda:NOW,source=None):
        return module.Dr007HistoryJob(catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE),
            source=source or MacroSourceDefinition.model_validate(SOURCE),transport=client,
            cursors=cursors,repository=repository.Dr007HistoryRepository(factory),lock=lock,clock=clock)
    yield factory,cursors,job,lock
    engine.dispose()


def run(job,**changes):
    return job.run(**{'as_of':NOW,'history_years':1,'end':None,'request_limit':2,**changes})


def test_budget_restart_and_no_other_state_or_publication(setup):
    factory,cursors,job,_=setup
    first=Client()
    report=run(job(first))
    assert (report['completedMonths'],report['requestCount'],report['rowCount']) == (2,2,2)
    assert report['start']=='2025-10-01' and report['end']=='2026-09-30'
    second=Client()
    assert run(job(second))['completedMonths']==4
    assert second.calls[0]['start_date']=='20260701'
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==4
        assert session.scalars(select(MacroSeriesState)).all()==[]
        assert session.scalars(select(PublishedSnapshot)).all()==[]


def test_cross_month_keeps_window_and_true_capture_time(setup):
    factory,_,job,_=setup
    run(job(Client()))
    later=datetime(2026,11,9,12,tzinfo=UTC)
    report=run(job(Client(),clock=lambda:later),as_of=later)
    assert report['end']=='2026-09-30' and report['asOf']=='2026-10-08T15:00:00+00:00'
    with factory() as session:
        row=session.scalar(select(MacroObservation).where(MacroObservation.observed_at==date(2026,7,1)))
        assert row.released_at.replace(tzinfo=UTC)==later


@pytest.mark.parametrize('empty,code',[(True,'DR007_HISTORY_EMPTY_MONTH'),(False,'TUSHARE_PERMISSION_DENIED')])
def test_empty_and_failure_rotate_without_core_or_secrets(setup,empty,code):
    factory,_,job,_=setup
    report=run(job(Client(empty=empty,fail=not empty)),request_limit=1)
    assert report['completedMonths']==0 and report['requestCount']==1
    assert report['failures']==[{'month':'2026-09','code':code}]
    assert 'controlled-token' not in str(report)
    second=Client()
    run(job(second),request_limit=1)
    assert second.calls[0]['start_date']=='20260801'
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==1


def test_core_commit_then_interrupt_reruns_idempotently(setup,monkeypatch):
    factory,cursors,job,_=setup
    original=cursors.save
    def interrupt(key,value):
        if any(e['status']=='done' for e in value['entries']):
            raise KeyboardInterrupt()
        original(key,value)
    monkeypatch.setattr(cursors,'save',interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(job(Client()),request_limit=1)
    monkeypatch.setattr(cursors,'save',original)
    result=run(job(Client()),request_limit=1)
    assert result['completedMonths']==1 and result['rowCount']==0
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==1


@pytest.mark.parametrize('mutation',['core_missing','core_value','core_unit','raw_missing','raw_content'])
def test_done_requires_real_evidence_and_status_remains_readonly(setup,mutation):
    factory,cursors,job,_=setup
    current=job(Client())
    run(current,request_limit=1)
    before=cursors.load('dr007-history-v1')
    with factory.begin() as session:
        core=session.scalar(select(MacroObservation))
        raw=session.scalar(select(SourcePayload))
        if mutation=='core_missing': session.delete(core)
        elif mutation=='core_value': core.value=9
        elif mutation=='core_unit': core.unit='bp'
        elif mutation=='raw_missing': session.delete(raw)
        else: raw.payload={**raw.payload,'status':'failed'}
    result=current.status()
    assert result['completedMonths']==0 and result['pendingMonths']==12
    assert result['failures']==[{'month':'2026-09','code':'DR007_HISTORY_EVIDENCE_INVALID'}]
    assert cursors.load('dr007-history-v1')==before


def test_completed_repeat_has_zero_http_and_zero_new_core(setup):
    factory,cursors,job,_=setup
    run(job(Client()),request_limit=12)
    no_http=Client()
    report=run(job(no_http),request_limit=12)
    assert report['status']=='completed' and report['confirmedObservations']==12
    assert report['requestCount']==0 and report['rowCount']==0 and no_http.calls==[]
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==12


@pytest.mark.parametrize('changes',[{'history_years':2},{'end':date(2026,8,31)}])
def test_config_drift_has_no_http_or_checkpoint_rewrite(setup,changes):
    _,cursors,job,_=setup
    run(job(Client()),request_limit=1)
    before=cursors.load('dr007-history-v1')
    client=Client()
    with pytest.raises(ValueError,match='^DR007_HISTORY_CONFIG_MISMATCH$'):
        run(job(client),**changes)
    assert client.calls==[] and cursors.load('dr007-history-v1')==before


def test_publication_lock_prevents_network_or_writes(setup):
    factory,cursors,job,lock=setup
    assert lock.try_acquire(data_publication_lock_key())
    try:
        client=Client()
        assert run(job(client))['status']=='locked' and client.calls==[]
        assert cursors.load('dr007-history-v1') is None
        with factory() as session:
            assert session.scalars(select(JobRun)).all()==[]
    finally: lock.release(data_publication_lock_key())


def test_manifest_addition_keeps_existing_govbond_source_identity():
    from app.macro.jobs.dr007_history import load_dr007_source
    from app.macro.jobs.govbond_history import load_govbond_source
    from app.macro.jobs.govbond_history_repository import fingerprint
    assert load_dr007_source().field=='weight'
    assert fingerprint(load_govbond_source().model_dump(mode='json',by_alias=True)) == (
        'bd4a97a94e4de8ded925598ce9e60dfb7bcd8a0de871fba5428d07efc3e08d0d')


def test_clearing_explicit_cutoff_cannot_bypass_config_drift(setup):
    factory,cursors,job,_=setup
    run(job(Client()),end=date(2026,8,31),request_limit=1)
    before=cursors.load('dr007-history-v1')
    client=Client()
    with factory() as session:
        original_runs=len(session.scalars(select(JobRun)).all())
        original_raw=len(session.scalars(select(SourcePayload)).all())
    with pytest.raises(ValueError,match='^DR007_HISTORY_CONFIG_MISMATCH$'):
        run(job(client),end=None,request_limit=1)
    assert client.calls==[] and cursors.load('dr007-history-v1')==before
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all())==original_runs
        assert len(session.scalars(select(SourcePayload)).all())==original_raw


def test_large_request_budget_still_bounds_batch_to_twelve_months(setup):
    _,_,job,_=setup
    client=Client()
    result=run(job(client),history_years=2,request_limit=100)
    assert result['completedMonths']==12 and result['pendingMonths']==12
    assert result['requestCount']==12 and len(client.calls)==12
