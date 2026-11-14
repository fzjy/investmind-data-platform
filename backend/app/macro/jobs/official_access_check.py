"""显式住户父失败访问检查；主库只读预检、独立持久诊断及原执行锁。"""
from sqlalchemy.engine import make_url

from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_extension_runtime import LOCK_KEY
from app.macro.extensions.config import load_extension_config
from app.macro.extensions.store import execution_schema
from app.macro.official_access_check import execute_official_access_check,validate_parent_id
from app.shared.readonly_database import open_readonly_factory


def run_official_access_check(settings,parent_id,*,dry_run=False,transport=None):
    validate_parent_id(parent_id)
    report=dict(status='blocked',readOnly=True,supplierHttp=0,additionalHttpBudget=1,
        insertedRows=0,publicationRequested=False,recoveryCompleted=False,parentRunId=parent_id)
    engine=None;lock=None;acquired=False
    try:
        if settings.data_backend!='database' or not settings.database_url:
            return {**report,'errorCode':'OFFICIAL_ACCESS_DATABASE_REQUIRED'}
        url=make_url(settings.database_url)
        if url.get_backend_name()!='postgresql' or url.get_driver_name()!='psycopg':
            return {**report,'errorCode':'OFFICIAL_ACCESS_POSTGRES_REQUIRED'}
        config=load_extension_config()
        if dry_run:
            with open_readonly_factory(settings.database_url) as factory:
                problems=execution_schema(factory)
                if problems:return {**report,'errorCode':'OFFICIAL_ACCESS_SCHEMA_REQUIRED','problems':problems}
                return execute_official_access_check(factory,parent_id,config,dry_run=True)
        engine,factory=create_engine_and_session(settings.database_url)
        lock=job_lock_for_engine(engine);acquired=lock.try_acquire(LOCK_KEY)
        if not acquired:return {**report,'status':'locked'}
        problems=execution_schema(factory)
        if problems:return {**report,'errorCode':'OFFICIAL_ACCESS_SCHEMA_REQUIRED','problems':problems}
        config=load_extension_config()
        from app.connectors.tushare.rate_limit import RequestRateLimiter
        limiter=RequestRateLimiter(config.groups['householdCredit'].limits.requestsPerMinute)
        report=execute_official_access_check(factory,parent_id,config,transport=transport,
            config_loader=load_extension_config,before_send=limiter.acquire)
    except Exception:
        report={**report,'status':'blocked','errorCode':'OFFICIAL_ACCESS_CHECK_FAILED'}
    finally:
        for stage,action in [('release',lambda:lock.release(LOCK_KEY) if acquired else None),
                             ('dispose',lambda:engine.dispose() if engine is not None else None)]:
            try:action()
            except Exception:report.setdefault('cleanupErrors',[]).append(stage)
    return report
