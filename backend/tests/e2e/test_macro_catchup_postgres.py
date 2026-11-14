"""在一次性PG验证跨连接周锁及提交中断恢复，非真实OS关机证据。"""
import os

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import JobRun
from app.jobs.locks import PostgresAdvisoryJobLock
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_macro_catchup import controlled_groups, execute, NOW, KEY, module
from backend.tests.jobs.test_macro_catchup_plan import seed, snapshot

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',
    reason='requires explicitly enabled disposable PostgreSQL17')]


def prepared(database):
    original,factory,engine=database
    seed(factory)
    return Settings(database_url=original.database_url,fred_api_key='controlled-key',tushare_token='controlled-token'),factory,engine


def test_pg_budget_resume_and_reentry_preserve_publication(database,monkeypatch):
    db=prepared(database)
    first,calls=execute(db,monkeypatch,limit=2)
    assert first['completedGroups']==1 and first['requestCount']==2 and not first['activeCoverageCompleted']
    second,calls=execute(db,monkeypatch)
    assert second['activeCoverageCompleted'] and second['requestCount']==16 and second['status']=='partial'
    before=snapshot(db[1])
    third,calls=execute(db,monkeypatch)
    assert third['status']=='skipped' and calls==[] and before==snapshot(db[1])


def test_pg_another_connection_lock_blocks_without_audit(database,monkeypatch):
    settings,factory,engine=prepared(database)
    controlled_groups(settings,factory,engine,monkeypatch)
    lock=PostgresAdvisoryJobLock(engine)
    assert lock.try_acquire('catchup-macro:'+KEY)
    before=snapshot(factory)
    try:
        result=module().run_macro_catchup(settings,transport=httpx.MockTransport(lambda r:pytest.fail('locked sent HTTP')),now=NOW)
    finally: lock.release('catchup-macro:'+KEY)
    assert result['status']=='locked' and snapshot(factory)==before


def test_pg_commit_then_shutdown_resumes_preserving_old_running_audit(database,monkeypatch):
    db=prepared(database)
    original=module()._save_progress
    def crash(*args):
        original(*args)
        raise KeyboardInterrupt('controlled shutdown')
    monkeypatch.setattr(module(),'_save_progress',crash)
    with pytest.raises(KeyboardInterrupt): execute(db,monkeypatch)
    with db[1]() as session:
        row=session.scalar(select(JobRun).where(JobRun.job_name=='catchup-macro'))
        original_id=row.id
        assert row.status=='running'
    monkeypatch.setattr(module(),'_save_progress',original)
    result,calls=execute(db,monkeypatch)
    assert result['attempt']==2 and result['requestCount']==16 and result['activeCoverageCompleted']
    with db[1]() as session: assert session.get(JobRun,original_id).status=='running'
