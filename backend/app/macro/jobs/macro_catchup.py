"""有界、可恢复的一次周补跑；暂停项不虚报完成，不自动发布。"""
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.config import Settings
from app.connectors.macro.budget import BudgetTransport, HttpRequestBudget
from app.db.models import JobRun
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.macro.jobs.macro_catchup_plan import read_macro_catchup_plan
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory, macro_sync_business_key
from app.macro.jobs.macro_scheduler import most_recent_macro_schedule
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync_planning import MacroSyncOptions
from app.macro.jobs.macro_sync_runtime import MacroSyncRuntime
from app.macro.jobs.us_rates_sync_runtime import SERIES_IDS as US_RATES, open_us_rates_sync_runtime
from app.macro.jobs.nbs_sync_runtime import SERIES_IDS as NBS, open_nbs_sync_runtime
from app.macro.jobs.tushare_macro_sync_runtime import SERIES_IDS as TUSHARE, open_tushare_macro_sync_runtime
from app.macro.jobs.fred_macro_sync_runtime import SERIES_IDS as FRED, open_fred_macro_sync_runtime
from app.macro.jobs.govbond_sync_runtime import SERIES_ID as GOVBOND, open_govbond_sync_runtime
from app.macro.jobs.dr007_sync_runtime import SERIES_ID as DR007, open_dr007_sync_runtime
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


@dataclass(frozen=True)
class MacroCatchupGroup:
    name: str
    series_ids: tuple[str, ...]
    opener: Callable[..., AbstractContextManager[MacroSyncRuntime]]


def macro_catchup_groups() -> tuple[MacroCatchupGroup, ...]:
    return (
        MacroCatchupGroup('sync-us-rates',US_RATES,open_us_rates_sync_runtime),
        MacroCatchupGroup('sync-govbond',(GOVBOND,),open_govbond_sync_runtime),
        MacroCatchupGroup('sync-dr007',(DR007,),open_dr007_sync_runtime),
        MacroCatchupGroup('sync-nbs',NBS,open_nbs_sync_runtime),
        MacroCatchupGroup('sync-tushare-macro',TUSHARE,open_tushare_macro_sync_runtime),
        MacroCatchupGroup('sync-fred-macro',FRED,open_fred_macro_sync_runtime),
    )


def _config_identity(settings: Settings) -> dict[str, object]:
    # 限额改变允许继续已证明分组；改变规划窗口或时区则重新验证全部组。
    return {'historyYears':settings.macro_history_years,
        'nbsLookbackMonths':settings.macro_nbs_lookback_months,
        'historyEnd':settings.macro_backfill_end.isoformat() if settings.macro_backfill_end else None,
        'weekday':settings.macro_sync_day_of_week,'time':settings.macro_sync_time.isoformat(),
        'timezone':settings.sync_timezone}


def _completed_groups(factory, key, catalog, groups, settings, now):
    states={s.series_id:s for s in MacroSeriesStateRepository(factory,catalog.catalog_version).get_all()}
    proven={}
    with factory() as session:
        runs=session.scalars(select(JobRun).where(JobRun.job_name=='catchup-macro',
            JobRun.business_key==key).order_by(JobRun.started_at.desc())).all()
        for group in groups:
            for run in runs:
                details=run.details
                if (details.get('catalogVersion')!=catalog.catalog_version
                        or details.get('catalogHash')!=catalog.catalog_hash
                        or details.get('config')!=_config_identity(settings)):
                    continue
                value=details.get('groups',{}).get(group.name)
                if not isinstance(value,dict) or value.get('status')!='completed':
                    continue
                source=session.get(JobRun,value.get('jobRunId'))
                if source is None or source.job_name!=group.name or source.status!='completed':
                    continue
                proof=source.details
                try: checked=datetime.fromisoformat(proof['asOf'])
                except (KeyError,ValueError,TypeError): continue
                if checked.utcoffset() is None or checked>now:
                    continue
                # 审计保留真实检查时刻；任务归属最近到期slot，不伪造前周asOf。
                due=most_recent_macro_schedule(checked,settings.macro_sync_day_of_week,
                    settings.macro_sync_time,settings.sync_timezone)
                if (source.business_key!=macro_sync_business_key(checked,catalog.catalog_version,settings.sync_timezone)
                        or key!=macro_sync_business_key(due,catalog.catalog_version,settings.sync_timezone)
                        or proof.get('catalogVersion')!=catalog.catalog_version
                        or proof.get('catalogHash')!=catalog.catalog_hash
                        or tuple(proof.get('seriesIds',()))!=group.series_ids
                        or proof.get('completed')!=len(group.series_ids)
                        or proof.get('rejectedCount',0)!=0 or proof.get('stale',0)!=0
                        or proof.get('missing',0)!=0 or proof.get('publicationRequested') is not False
                        or proof.get('unresolvedGaps')):
                    continue
                if any(sid not in states or states[sid].stale
                        or states[sid].catalog_version!=catalog.catalog_version for sid in group.series_ids):
                    continue
                proven[group.name]=value
                break
    return proven


def _save_progress(factory, run_id, groups, used):
    with factory.begin() as session:
        row=session.get(JobRun,run_id)
        row.details={**row.details,'groups':dict(groups)}
        row.request_count=used


def run_macro_catchup(settings: Settings, *, transport: httpx.BaseTransport | None = None,
                      now: datetime | None = None) -> dict[str, object]:
    instant=now if now is not None else datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError('MACRO_CATCHUP_AWARE_TIME_REQUIRED')
    instant=instant.astimezone(UTC)
    result={'status':'disabled','businessKey':None,'jobRunId':None,'requestCount':0,
        'httpLimit':settings.macro_catchup_request_limit,'completedGroups':0,
        'activeCoverageCompleted':False,'budgetExhausted':False,'publicationVersion':None}
    if not settings.macro_sync_enabled or not settings.macro_sync_catchup_on_start:
        return result
    # 只读预检不是运行时合同；真正工作仍通过各组锁内verify。
    plan=read_macro_catchup_plan(settings,now=instant)
    key=plan['weekly']['businessKey']
    result.update(businessKey=key,pausedSeries=plan['pausedSeries'])
    unsafe=[reason for reason in plan['blockingReasons']
        if reason!='WEEKLY_TOTAL_HTTP_BUDGET_NOT_IMPLEMENTED']
    if unsafe:
        return {**result,'status':'blocked','blockingReasons':unsafe}
    catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    groups=macro_catchup_groups()
    if (catalog.catalog_hash!=plan['catalogHash']
            or {sid for group in groups for sid in group.series_ids}
            !={s.id for s in catalog.series if s.sources}):
        raise ValueError('MACRO_CATCHUP_GROUP_CATALOG_MISMATCH')
    if not settings.database_url:
        raise ValueError('MACRO_CATCHUP_DATABASE_REQUIRED')
    with ExitStack() as resources:
        engine,factory=create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        lock=job_lock_for_engine(engine)
        lock_key='catchup-macro:'+key
        if not lock.try_acquire(lock_key):
            return {**result,'status':'locked'}
        resources.callback(lock.release,lock_key)
        # 取得周锁后重新读取，不能使用锁外快照作恢复授权。
        plan=read_macro_catchup_plan(settings,now=instant)
        unsafe=[reason for reason in plan['blockingReasons'] if reason!='WEEKLY_TOTAL_HTTP_BUDGET_NOT_IMPLEMENTED']
        if unsafe or plan['catalogHash']!=catalog.catalog_hash:
            return {**result,'status':'blocked','blockingReasons':unsafe or ['MACRO_CATALOG_CHANGED']}
        history=SqlMacroSyncHistory(factory,job_name='catchup-macro')
        _,attempt=history.inspect(key,catalog.catalog_version,catalog.catalog_hash)
        budget=HttpRequestBudget(settings.macro_catchup_request_limit)
        inner=transport if transport is not None else httpx.HTTPTransport(retries=0)
        if transport is None:
            resources.callback(inner.close)
        # 已证明组也必须重新验证当前合同/历史manifest；不能由检查点绕过门控。
        for group in groups:
            try:
                with group.opener(settings,transport=BudgetTransport(inner,budget)) as runtime:
                    options=MacroSyncOptions(instant,settings.macro_history_years,
                        catalog.catalog_version,catalog.catalog_hash,force=True,mode='live',
                        series_ids=group.series_ids,publish=False)
                    if not runtime.job.preflight(options):
                        return {**result,'status':'locked'}
            except Exception:
                return {**result,'status':'blocked','blockingReasons':['MACRO_GROUP_PREFLIGHT_FAILED'],
                    'blockedGroup':group.name}
        done=_completed_groups(factory,key,catalog,groups,settings,instant)
        if len(done)==len(groups):
            return {**result,'status':'skipped','completedGroups':len(done),
                'activeCoverageCompleted':True,'groups':done}
        run_id=history.start(key,instant,catalog.catalog_version,catalog.catalog_hash,'cli',attempt)
        with factory.begin() as session:
            row=session.get(JobRun,run_id)
            row.details={**row.details,'config':_config_identity(settings),'groups':done}
        entries=dict(done)
        error=None
        try:
            for group in groups:
                if group.name in done:
                    continue
                if budget.used>=budget.limit:
                    break
                before=budget.used
                try:
                    with group.opener(settings,transport=BudgetTransport(inner,budget)) as runtime:
                        if runtime.catalog.catalog_hash!=catalog.catalog_hash:
                            raise ValueError('MACRO_CATALOG_CHANGED')
                        report=runtime.job.run(MacroSyncOptions(instant,settings.macro_history_years,
                            catalog.catalog_version,catalog.catalog_hash,force=True,mode='live',
                            series_ids=group.series_ids,publish=False))
                    ok=(report.status=='completed' and report.completed==len(group.series_ids)
                        and not report.rejected_count and not report.failures)
                    entries[group.name]={'status':'completed' if ok else 'partial',
                        'jobRunId':report.job_run_id,'requestCount':budget.used-before}
                except Exception:
                    entries[group.name]={'status':'blocked','jobRunId':None,
                        'requestCount':budget.used-before,'errorCode':'MACRO_GROUP_PREFLIGHT_OR_RUN_FAILED'}
                _save_progress(factory,run_id,entries,budget.used)
        except Exception:
            error='MACRO_CATCHUP_INTERRUPTED'
        complete=sum(value['status']=='completed' for value in entries.values())
        active_complete=complete==len(groups)
        status='completed' if active_complete and not plan['pausedSeries'] else 'partial'
        payload={**result,'status':status,'jobRunId':run_id,'attempt':attempt,
            'requestCount':budget.used,'completedGroups':complete,'activeCoverageCompleted':active_complete,
            'budgetExhausted':budget.used>=budget.limit and not active_complete,'groups':entries,
            'errorCode':error,'config':_config_identity(settings)}
        history.finish(run_id,status,payload)
        return payload
