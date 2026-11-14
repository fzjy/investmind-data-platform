from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event, select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.plan import build_extension_plan, canonical
from app.macro.fiscal_store import FiscalStore, JOB
from app.macro.fiscal_view import load_fiscal_inputs
from backend.tests.macro.test_extension_client import response
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_fiscal_execution import setup, execute, body
from backend.tests.macro.test_fiscal_plan import NOW, DOCUMENTS


def failed_raw(factory, config, *, content=None):
    p = build_extension_plan('governmentBonds', config, NOW); s = FiscalStore(factory)
    key = s.reserve(p, p['documents'][0], NOW)
    s.receive(key, body(p['documents'][0]['year']) if content is None else content, NOW)
    s.fail(key, 'FISCAL_TABLE_INVALID', NOW)
    return key


def original_rows(factory, key):
    with factory() as session:
        job = session.get(JobRun, key); batch = session.get(IngestionBatch,key)
        raw = session.get(SourcePayload,job.details['rawId'])
        root = session.scalars(select(JobCursor).where(JobCursor.job_name == JOB)).one()
        return (job.status, job.request_count, job.row_count, job.error_message, job.finished_at,
            canonical(job.details), batch.status, batch.row_count, batch.request_count, batch.finished_at,
            canonical(raw.payload), raw.content_hash, canonical(root.cursor_value), root.updated_at)


def test_zero_http_replay_appends_receipt_without_changing_original_failure(database,tmp_path,monkeypatch):
    _, factory = database; c = setup(tmp_path,monkeypatch,lookback=3); key = failed_raw(factory,c)
    before = original_rows(factory,key); s = FiscalStore(factory)
    preview = s.replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=1),dry_run=True)
    assert preview['readOnly'] and preview['supplierHttp'] == 0 and count(factory,MacroObservation) == 0
    first = s.replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=1))
    assert first['status'] == 'completed' and first['supplierHttp'] == 0 and first['insertedRows'] == 3
    assert count(factory,JobRun) == 1 and count(factory,SourcePayload) == 1 and count(factory,JobCursor) == 2
    assert original_rows(factory,key) == before
    again = s.replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=2))
    assert again['readOnly'] and again['insertedRows'] == 0 and again['supplierHttp'] == 0
    assert s.inspect(build_extension_plan('governmentBonds',c,NOW),NOW+timedelta(seconds=2))['status'] == 'completed'


def test_replay_visibility_begins_at_actual_receipt_completion(database,tmp_path,monkeypatch):
    _, factory = database; c = setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=10))
    assert load_fiscal_inputs(factory,NOW+timedelta(seconds=9))['status'] == 'unavailable'
    assert load_fiscal_inputs(factory,NOW+timedelta(seconds=11))['status'] == 'available'


@pytest.mark.parametrize('cleanup_stage',['release','dispose'])
def test_committed_replay_report_survives_cleanup_failure(database,tmp_path,monkeypatch,cleanup_stage):
    from app.config import Settings
    from app.macro.jobs.macro_fiscal_runtime import run_fiscal_raw_replay
    import app.db.session as sessions
    import app.jobs.locks as locks
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    before=original_rows(factory,key); cleanup=[]
    class Engine:
        def dispose(self):
            cleanup.append('dispose')
            if cleanup_stage=='dispose': raise RuntimeError('controlled cleanup failure')
    class Lock:
        def try_acquire(self,key): return True
        def release(self,key):
            cleanup.append('release')
            if cleanup_stage=='release': raise RuntimeError('controlled cleanup failure')
    monkeypatch.setattr(sessions,'create_engine_and_session',lambda *_:(Engine(),factory))
    monkeypatch.setattr(locks,'job_lock_for_engine',lambda *_:Lock())
    settings=Settings(data_backend='database',database_url='postgresql+psycopg://localhost/offline')
    first=run_fiscal_raw_replay(settings,key)
    assert first['status']=='completed' and first['insertedRows']==3 and not first['readOnly']
    assert first['supplierHttp']==0 and first['cleanupErrors']==[cleanup_stage]
    assert cleanup==['release','dispose'] and original_rows(factory,key)==before
    again=run_fiscal_raw_replay(settings,key)
    assert again['status']=='completed' and again['readOnly'] and again['insertedRows']==0
    assert count(factory,MacroObservation)==3


def test_replayed_first_document_uses_only_four_remaining_original_slots(database,tmp_path,monkeypatch):
    _, factory=database; c=setup(tmp_path,monkeypatch); key=failed_raw(factory,c)
    FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW)
    calls=[]
    def handler(request):
        calls.append(request); year=next(d['year'] for d in DOCUMENTS if d['url']==str(request.url))
        assert year != 2022
        return response(200,content=body(year))
    first=execute(factory,c,transport=httpx.MockTransport(handler))
    again=execute(factory,c,transport=httpx.MockTransport(handler))
    assert first['status']=='completed' and first['supplierHttp']==4 and again['supplierHttp']==0
    assert count(factory,MacroObservation)==48 and count(factory,JobRun)==5 and len(calls)==4


def test_invalid_raw_cannot_create_replay_receipt(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c,content=b'<html>invalid</html>')
    with pytest.raises(ExtensionError): FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW)
    assert count(factory,MacroObservation)==0 and count(factory,JobCursor)==1


def test_no_raw_http_failure_cannot_be_replayed(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); s=FiscalStore(factory)
    p=build_extension_plan('governmentBonds',c,NOW); key=s.reserve(p,p['documents'][0],NOW)
    s.fail(key,'EXTENSION_AUTH_ERROR',NOW)
    with pytest.raises(ExtensionError): s.replay_failed_raw(key,clock=lambda:NOW)
    assert count(factory,MacroObservation)==count(factory,SourcePayload)==0 and count(factory,JobCursor)==1


def test_receipt_from_future_cannot_be_claimed_complete_on_clock_rollback(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    s=FiscalStore(factory); s.replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=10))
    with pytest.raises(ExtensionError): s.replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=9))


def test_clock_rollback_inside_replay_rolls_back_core_and_receipt(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    before=original_rows(factory,key); times=iter([NOW+timedelta(seconds=10),NOW+timedelta(seconds=1)])
    with pytest.raises(ExtensionError,match='EXTENSION_CLOCK_REGRESSION'):
        FiscalStore(factory).replay_failed_raw(key,clock=lambda:next(times))
    assert count(factory,MacroObservation)==0 and count(factory,JobCursor)==1
    assert original_rows(factory,key)==before


def test_replay_receipt_transaction_failure_rolls_back_core_and_preserves_raw(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    before=original_rows(factory,key)
    def fail_receipt(session,*args):
        if any(isinstance(obj,JobCursor) and obj.job_name != JOB for obj in session.new):
            raise RuntimeError('controlled receipt failure')
    event.listen(factory.class_,'before_flush',fail_receipt)
    try:
        with pytest.raises(RuntimeError): FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW)
    finally: event.remove(factory.class_,'before_flush',fail_receipt)
    assert count(factory,MacroObservation)==0 and count(factory,JobCursor)==1 and original_rows(factory,key)==before


def test_next_cycle_same_value_can_trace_a_replayed_origin(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW)
    from app.macro.jobs.macro_fiscal_runtime import execute_fiscal_group
    later=NOW+timedelta(days=8)
    result=execute_fiscal_group(factory,c,clock=lambda:later,transport=httpx.MockTransport(lambda _:response(200,content=body(2026))))
    assert result['status']=='completed' and result['insertedRows']==1 and count(factory,MacroObservation)==4


def test_replay_receipt_tampering_blocks_normal_execution(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    s=FiscalStore(factory); s.replay_failed_raw(key,clock=lambda:NOW)
    from app.macro.fiscal_store import RAW_REPLAY_JOB
    with factory.begin() as session:
        receipt=session.scalars(select(JobCursor).where(JobCursor.job_name==RAW_REPLAY_JOB)).one()
        receipt.cursor_value={**receipt.cursor_value,'contentHash':'0'*64}
    result=execute(factory,c,transport=httpx.MockTransport(lambda _:pytest.fail('cannot send')))
    assert result['status']=='blocked' and result['supplierHttp']==0


def test_rehashed_backdated_receipt_does_not_make_history_visible_early(database,tmp_path,monkeypatch):
    _,factory=database; c=setup(tmp_path,monkeypatch,lookback=3); key=failed_raw(factory,c)
    FiscalStore(factory).replay_failed_raw(key,clock=lambda:NOW+timedelta(seconds=10))
    from app.macro.fiscal_store import RAW_REPLAY_JOB
    from app.macro.extensions.plan import digest
    with factory.begin() as session:
        row=session.scalars(select(JobCursor).where(JobCursor.job_name==RAW_REPLAY_JOB)).one()
        value={k:v for k,v in row.cursor_value.items() if k!='receiptHash'}
        value['completedAt']=(NOW+timedelta(seconds=1)).isoformat()
        row.cursor_value={**value,'receiptHash':digest(value)}
    with pytest.raises(ExtensionError): load_fiscal_inputs(factory,NOW+timedelta(seconds=2))
