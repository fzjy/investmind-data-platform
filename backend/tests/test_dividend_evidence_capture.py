from backend.tests.support.snapshots import set_snapshot_version
from copy import deepcopy
import json

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import IngestionBatch, JobCursor, PublishedSnapshot, SourcePayload, Dividend

from backend.tests.marts.test_content_runtime import prepare, state


def runtime():
    import app.portfolio.dividend_capture as module
    return module


def config(budget=20):
    return {'maxHttpPerRun':budget,'timeoutSeconds':20}


def db_state(factory):
    with factory() as s:
        return (state(factory),[(r.id,r.job_name,r.cursor_key,deepcopy(r.cursor_value)) for r in s.scalars(select(JobCursor).order_by(JobCursor.id))],
            [(r.id,r.status,r.request_count,r.row_count) for r in s.scalars(select(IngestionBatch).order_by(IngestionBatch.id))],
            [(r.id,r.batch_id,r.content_hash,deepcopy(r.payload)) for r in s.scalars(select(SourcePayload).order_by(SourcePayload.id))],
            [(r.id,r.cash_div_tax,deepcopy(r.data)) for r in s.scalars(select(Dividend).order_by(Dividend.id))])


class Client:
    def __init__(self,factory,mutate=None):self.factory=factory;self.calls=[];self.mutate=mutate
    def set_before_request(self,callback):self.before=callback
    def call(self,api,params,fields):
        m=runtime()
        with self.factory() as s:
            reserved=s.scalar(select(JobCursor).where(JobCursor.job_name==m.JOB_NAME,JobCursor.cursor_key.endswith('unit:'+params['ts_code'])))
            assert reserved.cursor_value['status']=='reserved'
            assert s.get(IngestionBatch,reserved.cursor_value['batchId']).request_count==1
        self.before();self.calls.append((api,deepcopy(params),tuple(fields)))
        row=dict.fromkeys(fields);row.update(ts_code=params['ts_code'],end_date='20251231',ann_date='20260501',div_proc='实施',
            cash_div_tax=1,cash_div=.9,record_date='20260601',ex_date='20260602',pay_date='20260603',imp_ann_date='20260531',
            stk_div=0,stk_bo_rate=0,stk_co_rate=0,base_date='20260601',base_share=100)
        response=TushareResponse(fields=tuple(fields),items=(tuple(row[f] for f in fields),))
        return self.mutate(response) if self.mutate else response


def source(new_pg_factory):
    factory=new_pg_factory();prepare(factory)
    with factory.begin() as s:
        s.add(JobCursor(job_name='sync-stock-data',cursor_key='untouched',cursor_value={'status':'running','frozenFields':['old']}))
    return factory


def test_default_preview_is_zero_write_zero_http_and_dynamic_scope(new_pg_factory):
    f=source(new_pg_factory);before=db_state(f);c=Client(f)
    result=runtime().capture_dividend_evidence(f,c,config=config())
    assert result['status']=='planned' and result['requestCount']==0 and result['symbolCount']==1
    assert result['asOf']=='2026-08-24' and db_state(f)==before and not c.calls


def test_reservation_before_http_raw_only_and_same_target_reentry_zero(new_pg_factory):
    f=source(new_pg_factory);before=db_state(f);c=Client(f)
    result=runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='completed' and result['requestCount']==1
    after=db_state(f)
    assert before[0]==after[0] and before[4]==after[4]
    assert before[1][0]==after[1][0] and len(after[3])==len(before[3])+1
    assert runtime().capture_dividend_evidence(f,c,execute=True,config=config())['requestCount']==0
    assert db_state(f)==after and len(c.calls)==1


def test_physical_retry_is_prevented_by_pre_request_observer(new_pg_factory):
    f=source(new_pg_factory)
    class RetryClient(Client):
        def call(self,*args):
            response=super().call(*args)
            self.before() # 不得允许第二次真正HTTP。
            return response
    c=RetryClient(f)
    result=runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='blocked' and result['requestCount']==1 and len(c.calls)==1
    before=db_state(f)
    assert runtime().capture_dividend_evidence(f,c,execute=True,config=config())['requestCount']==0
    assert len(c.calls)==1 and db_state(f)==before


@pytest.mark.parametrize('bad',['wrong-symbol','missing-field','cap','nonfinite'])
def test_bad_decoded_response_saved_but_never_refetched(new_pg_factory, bad):
    f=source(new_pg_factory);before=db_state(f)
    def mutate(r):
        if bad=='missing-field':return TushareResponse(fields=r.fields[:-1],items=tuple(row[:-1] for row in r.items))
        if bad=='cap':return TushareResponse(fields=r.fields,items=r.items*2000)
        item=list(r.items[0]);item[r.fields.index('ts_code' if bad=='wrong-symbol' else 'stk_div')]='OTHER' if bad=='wrong-symbol' else 'NaN'
        return TushareResponse(fields=r.fields,items=(tuple(item),))
    c=Client(f,mutate)
    result=runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='blocked' and result['requestCount']==1
    after=db_state(f);assert len(after[3])==len(before[3])+1 and after[0]==before[0] and after[4]==before[4]
    assert runtime().capture_dividend_evidence(f,c,execute=True,config=config())['requestCount']==0
    assert len(c.calls)==1 and db_state(f)==after


def test_crash_leaves_reserved_unit_and_blocks_retry(new_pg_factory):
    f=source(new_pg_factory)
    class Crash(Client):
        def call(self,*args):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):runtime().capture_dividend_evidence(f,Crash(f),execute=True,config=config())
    c=Client(f);before=db_state(f)
    result=runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='blocked' and result['requestCount']==0 and not c.calls and db_state(f)==before


def test_parent_change_never_mixes_new_selection_into_frozen_target(new_pg_factory):
    f=source(new_pg_factory);c=Client(f)
    runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    with f.begin() as s:set_snapshot_version(s, s.get(PublishedSnapshot,'screener'), 'user-new-parent')
    result=runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='blocked' and result['code']=='CAPTURE_BASE_CHANGED' and len(c.calls)==1


@pytest.mark.parametrize('settings',[config(0),config(True),{'maxHttpPerRun':1,'timeoutSeconds':0}])
def test_invalid_budget_or_timeout_blocks_without_side_effect(new_pg_factory, settings):
    f=source(new_pg_factory);before=db_state(f)
    with pytest.raises(ValueError):runtime().capture_dividend_evidence(f,Client(f),execute=True,config=settings)
    assert db_state(f)==before


def test_cli_default_and_dry_run_priority(monkeypatch):
    from app.cli.main import app
    from app.config import Settings
    import app.cli.portfolio_rebuild as cli
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test')))
    class Engine:
        def dispose(self):pass
    monkeypatch.setattr(cli,'create_engine_and_session',lambda url:(Engine(),'factory'))
    called=[]
    monkeypatch.setattr(cli,'run_dividend_capture',lambda settings,factory,*,execute:(called.append(execute) or {'status':'planned'}))
    for args in [[],['--dry-run'],['--execute','--dry-run'],['--execute']]:
        assert CliRunner().invoke(app,['capture-portfolio-dividends',*args]).exit_code==0
    assert called==[False,False,False,True]


def test_budget_bounded_resume_does_not_repeat_completed_stock(new_pg_factory):
    f=source(new_pg_factory)
    with f.begin() as s:
        p=s.get(PublishedSnapshot,'screener');data=deepcopy(p.payload)
        row=deepcopy(data['stocks'][0]);row['symbol']='000001.SZ';data['stocks'].append(row);p.payload=data
    c=Client(f)
    first=runtime().capture_dividend_evidence(f,c,execute=True,config=config(1))
    assert first['status']=='pending' and first['requestCount']==1 and first['pendingCount']==1
    second=runtime().capture_dividend_evidence(f,c,execute=True,config=config(1))
    assert second['status']=='completed' and second['requestCount']==1
    assert {call[1]['ts_code'] for call in c.calls}=={'000001.SZ','600519.SH'} and len(c.calls)==2


def test_lock_conflict_and_tampered_evidence_cannot_issue_http(new_pg_factory, monkeypatch):
    from app.jobs.locks import ProcessLocalJobLock
    m=runtime();f=source(new_pg_factory);c=Client(f);lock=ProcessLocalJobLock()
    assert lock.try_acquire(m.JOB_NAME)
    monkeypatch.setattr(m,'job_lock_for_engine',lambda engine:lock)
    try:assert m.capture_dividend_evidence(f,c,execute=True,config=config())['code']=='CAPTURE_LOCK_BUSY'
    finally:lock.release(m.JOB_NAME)
    m.capture_dividend_evidence(f,c,execute=True,config=config())
    with f.begin() as s:
        p=s.scalar(select(SourcePayload).join(IngestionBatch,SourcePayload.batch_id==IngestionBatch.id).where(IngestionBatch.dataset==m.DATASET))
        payload=deepcopy(p.payload);payload['items']=[];p.payload=payload
    before=db_state(f)
    assert m.capture_dividend_evidence(f,c,execute=True,config=config())['code']=='CAPTURE_EVIDENCE_INVALID'
    assert len(c.calls)==1 and db_state(f)==before


def test_preview_separates_verified_raw_candidates_from_unchanged_core(new_pg_factory):
    from app.portfolio.dividend_report import preview_portfolio_dividends
    f=source(new_pg_factory);before=db_state(f);c=Client(f)
    runtime().capture_dividend_evidence(f,c,execute=True,config=config())
    saved=db_state(f)
    evidence=preview_portfolio_dividends(f)['capturedEvidence']
    assert evidence['status']=='completed' and evidence['completedCount']==1
    assert evidence['stocks'][0]['cashEventCount']==1 and evidence['stocks'][0]['capitalFieldsCompleteEvents']==1
    assert evidence['capitalWindowVerified'] is False and evidence['economicsReady'] is False
    assert evidence['stocks'][0]['events'][0]['sourceIds'][0].startswith('raw.source_payload:')
    assert db_state(f)==saved and before[4]==saved[4]


def test_changed_control_identity_cannot_reset_completed_unit_budget(new_pg_factory):
    f=source(new_pg_factory);c=Client(f);m=runtime()
    m.capture_dividend_evidence(f,c,execute=True,config=config())
    with f.begin() as s:
        row=s.scalar(select(JobCursor).where(JobCursor.job_name==m.JOB_NAME,JobCursor.cursor_key=='control'))
        data=deepcopy(row.cursor_value);data['identity']='different';row.cursor_value=data
    before=db_state(f)
    result=m.capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='blocked' and result['code']=='CAPTURE_IDENTITY_INVALID' and len(c.calls)==1
    assert db_state(f)==before


def test_rehashed_raw_cannot_disagree_with_accepted_unit_content_or_count(new_pg_factory):
    from app.ingestion.repositories import content_hash
    f=source(new_pg_factory);c=Client(f);m=runtime();m.capture_dividend_evidence(f,c,execute=True,config=config())
    with f.begin() as s:
        p=s.scalar(select(SourcePayload).join(IngestionBatch,SourcePayload.batch_id==IngestionBatch.id).where(IngestionBatch.dataset==m.DATASET))
        payload=deepcopy(p.payload);payload['items']=[];p.payload=payload;p.content_hash=content_hash(payload)
    before=db_state(f)
    assert m.capture_dividend_evidence(f,c,execute=True,config=config())['code']=='CAPTURE_EVIDENCE_INVALID'
    assert db_state(f)==before and len(c.calls)==1


@pytest.mark.parametrize('kind',['http500','nonjson','badfields','badwidth'])
def test_real_client_preserves_http_body_before_decode_failure(new_pg_factory, kind):
    import httpx
    from app.connectors.tushare.client import TushareClient
    f=source(new_pg_factory);m=runtime();calls=[]
    def handler(request):
        calls.append(request)
        if kind=='http500':return httpx.Response(500,content=b'server-error')
        if kind=='nonjson':return httpx.Response(200,content=b'not json')
        data={'fields':'not-list','items':[]} if kind=='badfields' else {'fields':['ts_code'],'items':[['600519.SH','extra']]}
        return httpx.Response(200,json={'code':0,'data':data})
    with TushareClient('fake-token',transport=httpx.MockTransport(handler),max_retries=0) as c:
        result=m.capture_dividend_evidence(f,c,execute=True,config=config())
        assert result['status']=='blocked' and result['requestCount']==1
        with f() as s:
            raw=s.scalar(select(SourcePayload).join(IngestionBatch,SourcePayload.batch_id==IngestionBatch.id).where(IngestionBatch.dataset==m.DATASET))
            assert raw is not None and raw.payload['_http']['bodyBase64']
            assert raw.payload['_http']['statusCode']==(500 if kind=='http500' else 200)
        before=db_state(f)
        assert m.capture_dividend_evidence(f,c,execute=True,config=config())['requestCount']==0
        assert db_state(f)==before and len(calls)==1


def test_credential_echo_in_response_is_retained_only_as_redacted_http_evidence(new_pg_factory):
    import base64
    import httpx
    from app.connectors.tushare.client import TushareClient
    f=source(new_pg_factory);m=runtime();token='fake-private-token'
    def handler(request):
        row=dict.fromkeys(m.DIVIDEND_FIELDS);row.update(ts_code='600519.SH',div_proc=token)
        return httpx.Response(200,json={'code':0,'data':{'fields':list(m.DIVIDEND_FIELDS),'items':[[row[k] for k in m.DIVIDEND_FIELDS]]}})
    with TushareClient(token,transport=httpx.MockTransport(handler),max_retries=0) as c:
        result=m.capture_dividend_evidence(f,c,execute=True,config=config())
    assert result['status']=='blocked'
    with f() as s:
        raw=s.scalar(select(SourcePayload).join(IngestionBatch,SourcePayload.batch_id==IngestionBatch.id).where(IngestionBatch.dataset==m.DATASET))
        assert token not in json.dumps(raw.payload)
        assert token.encode() not in base64.b64decode(raw.payload['_http']['bodyBase64'])
        assert raw.payload['_http']['credentialsRedacted'] is True
