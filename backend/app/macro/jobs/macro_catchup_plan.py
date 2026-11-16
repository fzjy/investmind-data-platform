"""启动补跑的只读证据预检；不构建供应商客户端、不运行job、不写周完成记录。"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from app.shared.readonly_database import open_readonly_factory

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.models import PublishedSnapshot
from app.macro.jobs.macro_scheduler import most_recent_macro_schedule, next_macro_schedule
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
from app.macro.jobs.macro_sync import _visible_state
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory, macro_sync_business_key
from app.macro.jobs.macro_sync_planning import MacroSyncOptions, MacroSyncPlanner, state_is_future
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.jobs.us_rates_sync_runtime import SERIES_IDS as US_RATES
from app.macro.jobs.nbs_sync_runtime import SERIES_IDS as NBS
from app.macro.jobs.tushare_macro_sync_runtime import SERIES_IDS as TUSHARE
from app.macro.jobs.fred_macro_sync_runtime import SERIES_IDS as FRED
from app.macro.jobs.govbond_sync_runtime import SERIES_ID as GOVBOND
from app.macro.jobs.dr007_sync_runtime import SERIES_ID as DR007
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


def read_macro_catchup_plan(settings: Settings, *, now: datetime | None = None) -> dict[str, object]:
    instant = now if now is not None else datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError('MACRO_CATCHUP_AWARE_TIME_REQUIRED')
    instant = instant.astimezone(UTC)
    if not settings.database_url:
        raise ValueError('MACRO_CATCHUP_DATABASE_REQUIRED')
    with open_readonly_factory(settings.database_url) as factory:
        return _read_plan(settings, instant, factory)


def _read_plan(settings: Settings, now: datetime, factory: sessionmaker) -> dict[str, object]:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    slot = most_recent_macro_schedule(now, settings.macro_sync_day_of_week,
        settings.macro_sync_time, settings.sync_timezone)
    key = macro_sync_business_key(slot, catalog.catalog_version, settings.sync_timezone)
    history = SqlMacroSyncHistory(factory)
    completed, attempt = history.inspect(key, catalog.catalog_version, catalog.catalog_hash)
    gaps = history.unresolved_gaps(catalog.catalog_version, catalog.catalog_hash)
    states = {state.series_id:state for state in MacroSeriesStateRepository(factory,catalog.catalog_version).get_all()}
    planner = MacroSyncPlanner(catalog, timezone=settings.sync_timezone)
    options = MacroSyncOptions(now,settings.macro_history_years,catalog.catalog_version,catalog.catalog_hash,mode='live')
    generic = {work.series.id:work.window for work in planner.plan(options,states)}
    groups = [
        ('sync-us-rates',US_RATES,settings.macro_us_rates_request_limit),
        ('sync-govbond',(GOVBOND,),None),('sync-dr007',(DR007,),None),
        ('sync-nbs',NBS,settings.macro_nbs_request_limit),
        ('sync-tushare-macro',TUSHARE,settings.macro_tushare_request_limit),
        ('sync-fred-macro',FRED,settings.macro_fred_request_limit),
    ]
    group_by_id = {sid:command for command,ids,_limit in groups for sid in ids}
    end = planner.end_date(now)
    observations = MacroObservationRepository(factory)
    rows = []
    blockers = {'WEEKLY_TOTAL_HTTP_BUDGET_NOT_IMPLEMENTED'}
    unsafe_evidence = False
    for definition in catalog.series:
        if not definition.sources:
            continue
        sid = definition.id
        state = states.get(sid)
        visible = observations.latest_visible(definition,now,end)
        reasons = []
        if state is None: reasons.append('MACRO_STATE_UNINITIALIZED')
        elif state.catalog_version != catalog.catalog_version: reasons.append('MACRO_STATE_CATALOG_MISMATCH')
        if state_is_future(state,now,end): reasons.append('MACRO_STATE_FUTURE')
        if visible is None: reasons.append('MACRO_CORE_NOT_VISIBLE')
        if gaps.get(sid): reasons.append('MACRO_ACTIVE_HISTORY_GAP')
        if sid not in group_by_id: reasons.append('MACRO_SCOPED_GROUP_UNAVAILABLE')
        primary = min(definition.sources,key=lambda source:source.priority).provider
        if ((primary == 'fred' and not settings.fred_api_key)
                or (primary == 'tushare' and not settings.tushare_token)):
            reasons.append('MACRO_CREDENTIAL_MISSING')
        blockers.update(reasons)
        unsafe_evidence = unsafe_evidence or bool(reasons)
        # 与现有专用job一致：可见core可形成临时规划state，但不会写回。
        scoped_states = dict(states)
        if visible is not None:
            scoped_states[sid] = _visible_state(sid,catalog.catalog_version,state,visible)
        scoped_catalog = catalog
        if sid in NBS:
            overlap = catalog.defaults.overlap.model_copy(update={'monthly_months':settings.macro_nbs_lookback_months})
            scoped_catalog = catalog.model_copy(update={'defaults':catalog.defaults.model_copy(update={'overlap':overlap})})
        scoped_options = replace(options,series_ids=(sid,),publish=False)
        scoped = MacroSyncPlanner(scoped_catalog,timezone=settings.sync_timezone).plan(scoped_options,scoped_states)[0].window
        def window_payload(window):
            return {'start':window.start.isoformat(),'end':window.end.isoformat(),'asOf':window.as_of.isoformat()}
        rows.append({'seriesId':sid,'group':group_by_id.get(sid),'primaryProvider':primary,
            'coreVisible':visible is not None,'genericWindow':window_payload(generic[sid]),
            'scopedWindow':window_payload(scoped),'blockingReasons':reasons,
            'unresolvedGaps':[{'start':start.isoformat(),'end':last.isoformat()} for start,last in gaps.get(sid,())]})
    with factory() as session:
        published = session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(
            ('macro-insights','macro-transmission')))).all()
    decision = ('disabled' if not settings.macro_sync_enabled or not settings.macro_sync_catchup_on_start
        else 'already_completed' if completed else 'blocked' if unsafe_evidence else 'needs_bounded_startup')
    # 延迟导入只读检查点算法，避免计划/执行模块初始化互相依赖。
    # 仍在同一个外部只读事务中，不构造客户端或运行任何job。
    from app.macro.jobs.macro_catchup import _completed_groups, macro_catchup_groups
    catchup_groups = macro_catchup_groups()
    proven = _completed_groups(factory,key,catalog,catchup_groups,settings,now)
    return {'readOnly':True,'requestCount':0,'checkedAt':now.isoformat(),'decision':decision,
        'catalogVersion':catalog.catalog_version,'catalogHash':catalog.catalog_hash,
        'weekly':{'enabled':settings.macro_sync_enabled,'catchupEnabled':settings.macro_sync_catchup_on_start,
            'latestDueAt':slot.isoformat(),'nextRunAt':next_macro_schedule(now,settings.macro_sync_day_of_week,
                settings.macro_sync_time,settings.sync_timezone).isoformat(),
            'businessKey':key,'completed':completed,'nextAttempt':attempt},
        'activeCount':len(rows),'pausedSeries':[d.id for d in catalog.series if not d.sources],
        'series':rows,'blockingReasons':sorted(blockers),'weeklyExecutionReady':False,'totalHttpLimit':None,
        'groups':[{'command':command,'seriesIds':list(ids),'httpLimit':limit,
            'runtimePreflightStillRequired':True} for command,ids,limit in groups],
        'publication':{'products':sorted(p.product for p in published),
            'versions':sorted({p.version for p in published})},
        'boundedCatchup':{'command':'catchup-macro','httpLimit':settings.macro_catchup_request_limit,
            'completedGroups':len(proven),'activeScopeCompleted':len(proven)==len(catchup_groups),
            'runtimePreflightStillRequired':True},
        'warnings':['PAUSED_SOURCES_PREVENT_FULL_WEEKLY_COMPLETION',
            'GROUP_LIMITS_ARE_NOT_A_GLOBAL_HTTP_CAP','PLAN_WINDOWS_ARE_NOT_HTTP_REQUESTS',
            'HISTORY_MANIFEST_AND_SOURCE_CONTRACT_PREFLIGHT_STILL_REQUIRED']}
