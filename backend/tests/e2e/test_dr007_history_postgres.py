"""一次性 PG17 事务恢复；供应商HTTP完整受控，不接业务库。"""
import json
import os

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import select

from app.db.models import JobCursor, MacroObservation, MacroSeriesState, PublishedSnapshot
from app.jobs.cursors import SqlJobCursorRepository
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_dr007_history import Client, NOW

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',
    reason='requires disposable PostgreSQL 17')]


def handler(client):
    def handle(request):
        body=json.loads(request.content)
        assert body['token']=='controlled-token' and body['api_name']=='repo_daily'
        result=client.call(body['api_name'],body['params'],tuple(body['fields'].split(',')))
        return httpx.Response(200,json={'code':0,'msg':'','data':result.model_dump(mode='json')})
    return httpx.MockTransport(handle)


def run(runtime):
    runtime.job.clock=lambda:NOW
    return runtime.job.run(as_of=NOW,history_years=1,end=None,request_limit=1)


def test_postgres_new_runtime_resumes_and_keeps_old_products_and_cursors(database):
    from app.macro.jobs.dr007_history_runtime import open_dr007_history_runtime,read_dr007_history_status
    settings,factory,_=database
    settings=settings.model_copy(update={'tushare_token':SecretStr('controlled-token')})
    old=SqlJobCursorRepository(factory,job_name='backfill-govbond-history')
    old.save('govbond-history-v1',{'protected':'old'})
    with open_dr007_history_runtime(settings,transport=handler(Client())) as runtime:
        assert run(runtime)['rowCount']==1
    assert read_dr007_history_status(settings)['completedMonths']==1
    second=Client()
    with open_dr007_history_runtime(settings,transport=handler(second)) as runtime:
        assert run(runtime)['rowCount']==1
    assert second.calls[0]['start_date']=='20260801'
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==2
        assert session.scalars(select(MacroSeriesState)).all()==[]
        assert session.scalars(select(PublishedSnapshot)).all()==[]
        cursor=session.scalar(select(JobCursor).where(JobCursor.cursor_key=='govbond-history-v1'))
        assert cursor.cursor_value=={'protected':'old'}


def test_postgres_core_commit_then_interrupt_is_idempotent(database,monkeypatch):
    from app.macro.jobs.dr007_history_runtime import open_dr007_history_runtime
    settings,factory,_=database
    settings=settings.model_copy(update={'tushare_token':SecretStr('controlled-token')})
    with open_dr007_history_runtime(settings,transport=handler(Client())) as runtime:
        original=runtime.job.cursors.save
        def interrupt(key,value):
            if any(e['status']=='done' for e in value['entries']):
                raise KeyboardInterrupt()
            original(key,value)
        monkeypatch.setattr(runtime.job.cursors,'save',interrupt)
        with pytest.raises(KeyboardInterrupt): run(runtime)
    with open_dr007_history_runtime(settings,transport=handler(Client())) as runtime:
        result=run(runtime)
        assert result['rowCount']==0 and result['completedMonths']==1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all())==1
