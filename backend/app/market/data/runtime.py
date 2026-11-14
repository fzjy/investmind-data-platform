"""CLI/日常入口共用；只读预检不创建客户端，发布必须显式请求。"""
from contextlib import ExitStack
from datetime import datetime

from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.jobs.run_errors import JobLockedError, JobConfigurationError
from app.market.data.catalog import MarketDataCatalog
from app.market.data.publication import publish_market_quotes
from app.market.data.sync import MarketDataSync
from app.shared.readonly_database import open_readonly_factory
from app.market.data.schedule_repository import MarketScheduleRepository
from app.market.data.scheduling import most_recent_market_schedule
from app.market.data.catalog import plan_market_targets


def run_market_step(settings, *, now: datetime, dry_run: bool, publish: bool) -> dict:
    catalog = MarketDataCatalog.load()
    if not catalog.enabled:
        return {'status': 'disabled', 'requestCount': 0, 'publishReady': False}
    # 周期检查只读；实际执行另开普通连接，禁止从只读事务内写入。
    with open_readonly_factory(settings.database_url) as factory:
        info = MarketScheduleRepository(factory).inspect(catalog, now=now)
    if info['collected'] and (not publish or info['published']):
        return {**info, 'status': 'planned' if dry_run else 'not_due', 'requestCount': 0, 'publishReady': True}
    if dry_run:
        frozen = MarketScheduleRepository.targets(catalog, info['targets']) or None
        return {**info, **run_market_update(settings, now=now, dry_run=True, publish=publish, targets=frozen,
            planned_at=datetime.fromisoformat(info['plannedAt']) if info['plannedAt'] else None)}
    # 已采未发的同周期目标必须冻结，不能因早上美股目标推进重新采集。
    if info['collected']:
        with ExitStack() as resources:
            engine, factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
            targets = MarketScheduleRepository.targets(catalog, info['targets'])
            publication = publish_market_quotes(factory, catalog, checked_at=now, targets=targets,
                planned_at=datetime.fromisoformat(info['plannedAt']))
            report = {'status': 'completed' if publication['status'] in {'completed','no_change'} else 'partial',
                'requestCount': 0, 'publishReady': True, 'publication': publication}
            MarketScheduleRepository(factory).record(catalog, slot=datetime.fromisoformat(info['latestDueAt']),
                targets=targets, checked_at=now, collection={'status': 'completed'}, publication=publication)
            return {**info, **report}
    targets = MarketScheduleRepository.targets(catalog,info['targets']) or plan_market_targets(catalog,now)
    slot = datetime.fromisoformat(info['latestDueAt'])
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        MarketScheduleRepository(factory).begin_attempt(catalog,slot=slot,targets=targets,checked_at=now)
    report = run_market_update(settings, now=now, dry_run=False, publish=publish, targets=targets,
        planned_at=datetime.fromisoformat(info['plannedAt']) if info['plannedAt'] else now)
    if report.get('publishReady'):
        with ExitStack() as resources:
            engine, factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
            MarketScheduleRepository(factory).record(catalog, slot=most_recent_market_schedule(now,catalog.schedule),
                targets=targets, checked_at=now, collection={'status':'completed'},
                publication=report.get('publication'))
    return {**info, **report}


def run_market_update(settings, *, now: datetime, dry_run: bool = False, publish: bool = False, targets=None, planned_at=None) -> dict:
    try:
        catalog = MarketDataCatalog.load()
        if settings.data_backend != 'database' or not settings.database_url or now.utcoffset() is None:
            return {'status': 'configuration_error', 'code': 'MARKET_DATABASE_AND_CLOCK_REQUIRED', 'requestCount': 0}
    except Exception:
        return {'status': 'configuration_error', 'code': 'MARKET_CONFIGURATION_INVALID', 'requestCount': 0}
    if not catalog.enabled:
        return {'status': 'disabled', 'requestCount': 0, 'publishReady': False}
    try:
        with ExitStack() as resources:
            if dry_run:
                factory = resources.enter_context(open_readonly_factory(settings.database_url))
                return MarketDataSync(factory, None, catalog, None, targets=targets, planned_at=planned_at).preflight(now)
            engine, factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
            lock = job_lock_for_engine(engine)
            sync = MarketDataSync(factory, None, catalog, lock, targets=targets, planned_at=planned_at)
            plan = sync.preflight(now)
            if plan['pendingCount']:
                if settings.tushare_token is None:
                    return {'status': 'configuration_error', 'code': 'MARKET_CREDENTIAL_REQUIRED', 'requestCount': 0}
                rpm = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
                sync.client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
                    timeout_seconds=catalog.sync.timeout_seconds, max_retries=catalog.sync.max_retries,
                    rate_limiter=RequestRateLimiter(rpm)))
            report = sync.run(now).to_payload()
            report.update(publishReady=report['status'] == 'completed', targets=plan['targets'])
            if publish and report['publishReady']:
                result = publish_market_quotes(factory, catalog, checked_at=now, targets=targets, planned_at=planned_at)
                report['publication'] = result
                if result['status'] not in {'completed','no_change'}:
                    report['status'] = 'locked' if result['status'] == 'locked' else 'partial'
            return report
    except JobLockedError:
        return {'status': 'locked', 'code': 'MARKET_LOCK_BUSY', 'requestCount': 0}
    except (ValueError, JobConfigurationError) as exc:
        code = str(exc) if str(exc) in {'MARKET_MIGRATION_REQUIRED','MARKET_CREDENTIAL_REQUIRED'} else 'MARKET_CONFIGURATION_INVALID'
        return {'status': 'configuration_error', 'code': code, 'requestCount': 0 if dry_run else None}
    except Exception:
        return {'status': 'failed', 'code': 'MARKET_UPDATE_FAILED', 'requestCount': 0 if dry_run else None}
