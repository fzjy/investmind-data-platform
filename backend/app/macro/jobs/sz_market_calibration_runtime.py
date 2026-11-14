"""仅从已保存采样派生，不读取token或创建供应商传输会话。"""
from datetime import datetime, timezone

from sqlalchemy.engine import make_url

from app.db.session import create_engine_and_session
from app.macro.extensions.config import load_extension_config
from app.macro.sz_market_calibration import CalibrationStore
from app.macro.sz_market_sample import build_sample_plan
from app.shared.readonly_database import open_readonly_factory


def run_sz_market_calibration(settings, *, dry_run=False):
    base = {'readOnly': True, 'supplierHttp': 0, 'publicationRequested': False}
    if settings.data_backend != 'database' or not settings.database_url:
        return {**base, 'status': 'blocked', 'errorCode': 'MACRO_EXTENSION_DATABASE_REQUIRED'}
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'postgresql' or url.get_driver_name() != 'psycopg':
        return {**base, 'status': 'blocked', 'errorCode': 'EXTENSION_POSTGRES_REQUIRED'}
    def execute(factory):
        policy = load_extension_config().contractSamples.get('szMarket')
        if policy is None or not policy.enabled:
            return {**base, 'status': 'disabled'}
        now = datetime.now(timezone.utc)
        # 配置仅决定原采样身份；不将enabled或单位解释改写进冻结原件。
        from app.macro.sz_market_sample_store import SzMarketSampleStore
        plan = build_sample_plan(factory, policy, now)
        if SzMarketSampleStore(factory).inspect(plan, now)['status'] != 'captured':
            return {**base, 'status': 'blocked', 'errorCode': 'SZ_CALIBRATION_SAMPLE_REQUIRED'}
        # 真实命令在取得事务锁后再读时钟；等待者不能用锁前截止时间误报新提交损坏。
        return CalibrationStore(factory).replay(now, dry_run=dry_run, clock=lambda: datetime.now(timezone.utc))
    if dry_run:
        with open_readonly_factory(settings.database_url) as factory: return execute(factory)
    engine, factory = create_engine_and_session(settings.database_url)
    try: return execute(factory)
    finally: engine.dispose()
