"""保存官方原件的零HTTP正式恢复入口；不增加供应商请求。"""
from datetime import datetime,timezone
from sqlalchemy.engine import make_url

from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_extension_runtime import LOCK_KEY
from app.macro.extensions.config import load_extension_config
from app.macro.extensions.store import execution_schema
from app.macro.official_access_check import OfficialAccessCheckStore,validate_parent_id
from app.macro.official_access_replay import OfficialAccessReplayStore
from app.macro.official_macro_store import OfficialMacroStore
from app.shared.readonly_database import open_readonly_factory


def run_official_access_replay(settings,parent_id,*,dry_run=False):
    validate_parent_id(parent_id)
    report=dict(status='blocked',readOnly=True,supplierHttp=0,insertedRows=0,
        publicationRequested=False,recoveryCompleted=False,parentRunId=parent_id)
    engine=None;lock=None;acquired=False
    try:
        if settings.data_backend!='database' or not settings.database_url:
            return {**report,'errorCode':'OFFICIAL_REPLAY_DATABASE_REQUIRED'}
        url=make_url(settings.database_url)
        if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':
            return {**report,'errorCode':'OFFICIAL_REPLAY_POSTGRES_REQUIRED'}
        if dry_run:
            with open_readonly_factory(settings.database_url) as factory:
                if execution_schema(factory):return {**report,'errorCode':'OFFICIAL_REPLAY_SCHEMA_REQUIRED'}
                state=OfficialAccessReplayStore(factory,parent_id).inspect(datetime.now(timezone.utc))
                if state['status']!='completed':OfficialAccessCheckStore(factory,parent_id).inspect(load_extension_config(),datetime.now(timezone.utc))
                return {**report,'status':'preview','sourceStatus':state['status'],'runId':state['runId'],'source':state['source']}
        engine,factory=create_engine_and_session(settings.database_url)
        lock=job_lock_for_engine(engine);acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:return {**report,'status':'locked'}
        if execution_schema(factory):return {**report,'errorCode':'OFFICIAL_REPLAY_SCHEMA_REQUIRED'}
        replay=OfficialAccessReplayStore(factory,parent_id);state=replay.inspect(datetime.now(timezone.utc))
        report.update(runId=state['runId'],source=state['source'])
        if state['status']=='completed':return {**report,'status':'completed','recoveryCompleted':True}
        config=load_extension_config()
        OfficialAccessCheckStore(factory,parent_id).inspect(config,datetime.now(timezone.utc))
        report.update(readOnly=False,phase='unknown',recoveryCompleted=None,insertedRows=None)
        key=replay.prepare(config,datetime.now(timezone.utc))
        OfficialAccessCheckStore(factory,parent_id).inspect(load_extension_config(),datetime.now(timezone.utc))
        result=OfficialMacroStore(factory).replay(key,datetime.now(timezone.utc))
        report['insertedRows']=result.get('insertedRows',0)
        final=replay.inspect(datetime.now(timezone.utc))
        if final['status']!='completed':raise ValueError('OFFICIAL_REPLAY_NOT_COMPLETE')
        report.update(status='completed',phase='completed',recoveryCompleted=True)
    except Exception:
        report={**report,'status':'blocked','errorCode':'OFFICIAL_REPLAY_EXECUTION_FAILED'}
    finally:
        for name,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),
                            ('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:report.setdefault('cleanupErrors',[]).append(name)
    return report
