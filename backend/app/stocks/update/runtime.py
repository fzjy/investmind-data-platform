"""证券日常组入口；复用原有检查点、限额与发布规则。"""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import date, datetime
from app.config import Settings
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.rate_limit import RequestRateLimiter
from app.db.session import create_engine_and_session
from app.jobs.locks import job_lock_for_engine
from app.jobs.run_errors import JobConfigurationError, JobLockedError
from app.shared.readonly_database import open_readonly_factory
from app.stocks.update.planning import StockUpdateOptions
from app.stocks.update.publication import publish_stock_update
from app.stocks.update.sync import StockUpdateSync
from app.stocks.universes import UniverseResolver


def run_stock_step(settings: Settings, *, now: datetime, dry_run: bool, publish: bool) -> dict:
    options = StockUpdateOptions(settings.stock_update_as_of,
        settings.stock_update_max_requests_per_run, settings.stock_update_calendar_lookahead_days,
        settings.backfill_market_years, settings.sync_timezone, settings.sync_hour, settings.sync_minute)
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory:
            return {**StockUpdateSync(factory, None, UniverseResolver().resolve('representative-50'),
                options, None).preflight(now), 'status': 'planned', 'requests': 0}
    if settings.tushare_token is None:
        raise JobConfigurationError('Tushare Token未配置')
    with ExitStack() as resources:
        engine, factory = create_engine_and_session(settings.database_url)
        resources.callback(engine.dispose)
        lock = job_lock_for_engine(engine)
        pool = UniverseResolver().resolve('representative-50')
        job = StockUpdateSync(factory, None, pool, options, lock)
        rate = (settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip'
            else settings.tushare_standard_requests_per_minute)
        job.client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),
            rate_limiter=RequestRateLimiter(rate)))
        result = job.run(now)
        if publish and result['status'] == 'completed' and result['publishReady']:
            try:
                result['publication'] = asdict(publish_stock_update(factory, pool,
                    date.fromisoformat(result['asOf']), lock, collection=result, options=options))
            except JobLockedError:
                result['publication'] = {'status': 'locked', 'errorCode': 'ROUTINE_STOCK_PUBLICATION_LOCKED'}
            except ValueError:
                result['publication'] = {'status': 'blocked', 'errorCode': 'ROUTINE_STOCK_PUBLICATION_NOT_READY'}
            except Exception:
                result['publication'] = {'status': 'failed', 'errorCode': 'ROUTINE_STOCK_PUBLICATION_FAILED'}
        return result
