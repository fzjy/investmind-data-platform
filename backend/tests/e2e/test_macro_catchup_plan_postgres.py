"""一次性PG17核对预检的只读事务和跨查询一致视图，不连接业务库。"""
import json
import os
from datetime import timedelta

import pytest
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from app.config import Settings
from app.db.models import JobRun, MacroSeriesState
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from backend.tests.e2e.test_macro_history_postgres import database
from backend.tests.jobs.test_macro_catchup_plan import NOW, KEY, seed, snapshot, module, freeze_clock

pytestmark = [pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',
    reason='requires explicitly enabled disposable PostgreSQL17')]


def prepared(database):
    original,factory,engine=database
    seed(factory)
    return Settings(database_url=original.database_url,fred_api_key='controlled-key',
        tushare_token='controlled-token'),factory,engine


def test_pg_real_cli_preflight_preserves_all_existing_rows(database,monkeypatch):
    from app.cli import main
    settings,factory,_=prepared(database)
    before=snapshot(factory)
    monkeypatch.setattr(main.Settings,'from_env',lambda:settings)
    freeze_clock(monkeypatch)
    result=CliRunner().invoke(main.app,['plan-macro-catchup'])
    assert result.exit_code==0, result.output
    report=json.loads(result.stdout)
    assert report['requestCount']==0 and report['activeCount']==18 and report['readOnly']
    assert report['decision']=='needs_bounded_startup' and not report['weekly']['completed']
    assert report['weekly']['businessKey']==KEY
    assert report['weekly']['latestDueAt']=='2026-10-05T01:00:00+00:00'
    assert snapshot(factory)==before


def test_pg_database_rejects_accidental_writer_in_preflight(database,monkeypatch):
    settings,factory,_=prepared(database)
    before=snapshot(factory)
    def write(_settings,_now,read_factory):
        with read_factory.begin() as s:
            s.add(JobRun(id='must-not-write',job_name='sync-macro',business_key='forbidden',status='completed'))
            s.flush()
    monkeypatch.setattr(module(),'_read_plan',write)
    with pytest.raises(DBAPIError) as failure:
        module().read_macro_catchup_plan(settings,now=NOW)
    assert failure.value.orig.sqlstate=='25006'
    assert snapshot(factory)==before


def test_pg_repeatable_read_keeps_plan_consistent_across_external_update(database,monkeypatch):
    settings,factory,_=prepared(database)
    original=SqlMacroSyncHistory.inspect
    def interleaved(self,*args):
        result=original(self,*args)
        with factory.begin() as s:
            s.get(MacroSeriesState,'us.cpi.yoy').last_success_at=NOW+timedelta(days=1)
        return result
    monkeypatch.setattr(SqlMacroSyncHistory,'inspect',interleaved)
    report=module().read_macro_catchup_plan(settings,now=NOW)
    row=next(r for r in report['series'] if r['seriesId']=='us.cpi.yoy')
    assert 'MACRO_STATE_FUTURE' not in row['blockingReasons']
    with factory() as s: assert s.get(MacroSeriesState,'us.cpi.yoy').last_success_at==NOW+timedelta(days=1)
