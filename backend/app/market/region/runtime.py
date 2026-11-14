"""默认预检只读；正常入口只补既有固定池的地域字段。"""
from contextlib import ExitStack
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.market.data.catalog import MarketDataCatalog
from app.market.data.scheduling import most_recent_market_schedule
from app.market.region.sync import RegionSync
from app.shared.readonly_database import open_readonly_factory
from app.stocks.api.universe import UniverseResolver


def run_region_step(settings, *, now, dry_run, publish):
    plan = run_region_update(settings, now=now, dry_run=True, publish=publish)
    if plan['status'] != 'planned':
        return plan
    catalog = MarketDataCatalog.load()
    due = most_recent_market_schedule(now,catalog.schedule).astimezone(ZoneInfo(catalog.schedule.timezone))
    if date.fromisoformat(plan['asOf']) > due.date():
        return {**plan,'status':'planned' if dry_run else 'not_due','code':'REGION_DAILY_TIME_NOT_DUE'}
    return plan if dry_run else run_region_update(settings,now=now,dry_run=False,publish=publish)


def run_region_update(settings, *, now, dry_run=False, publish=False):
    if (settings.data_backend != 'database' or not settings.database_url or now.utcoffset() is None
        or settings.sync_universe != 'representative-50' or settings.sync_universe_file is not None):
        return {'status':'configuration_error','code':'REGION_FIXED_DATABASE_SCOPE_REQUIRED','requestCount':0}
    try:
        catalog = MarketDataCatalog.load()
        selection = UniverseResolver().resolve('representative-50')
    except Exception:
        return {'status':'configuration_error','code':'REGION_CONFIGURATION_INVALID','requestCount':0}
    try:
        with ExitStack() as resources:
            if dry_run:
                factory = resources.enter_context(open_readonly_factory(settings.database_url))
                return RegionSync(factory,None,selection,catalog,None).preflight(now)
            engine,factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
            sync = RegionSync(factory,None,selection,catalog,job_lock_for_engine(engine),clock=lambda:datetime.now(UTC))
            plan = sync.preflight(now)
            if plan['status']=='disabled':
                return plan
            if plan['pendingCount']:
                if settings.tushare_token is None:
                    return {'status':'configuration_error','code':'REGION_CREDENTIAL_REQUIRED','requestCount':0}
                rpm = settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip' else settings.tushare_standard_requests_per_minute
                sync.client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
                    timeout_seconds=catalog.region.timeout_seconds,max_retries=0,rate_limiter=RequestRateLimiter(rpm)))
            return sync.run(now,publish=publish)
    except ValueError as exc:
        code = str(exc) if str(exc) in {'PULSE_PUBLISHED_STOCKS_REQUIRED','PULSE_STOCK_SCOPE_INCOMPLETE',
            'PULSE_FUTURE_TARGET','REGION_CREDENTIAL_REQUIRED'} else 'REGION_CONFIGURATION_INVALID'
        return {'status':'blocked','code':code,'requestCount':0 if dry_run else None}
    except Exception:
        return {'status':'failed','code':'REGION_UPDATE_FAILED','requestCount':0 if dry_run else None}
