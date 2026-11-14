"""后端生命周期托管任务；HTTP处理与阻塞采集分离。"""
import asyncio
import logging
from contextlib import asynccontextmanager
from threading import Event
from app.config import Settings
from app.content.news_runtime import run_news
from smartparse_news import Config

logger = logging.getLogger(__name__)


async def news_loop(stop, *, settings_loader=Settings.from_env, runner=run_news):
    delay = Config().worker_retry_seconds
    while not stop.is_set():
        try:
            settings = settings_loader()
            config = Config.read(settings.news_config_path)
            delay = config.worker_retry_seconds
            if config.enabled:
                result = await asyncio.to_thread(runner, settings, publish=True,
                    stopped=stop.is_set, sleep=stop.wait)
                logger.log(logging.WARNING if result['status'] in ('failed', 'blocked', 'delivery_failed') else logging.INFO,
                    'news_worker status=%s requests=%s code=%s', result['status'], result.get('requestCount', 0), result.get('code'))
                if result['status'] not in ('failed', 'blocked', 'delivery_failed'):
                    delay = config.interval_seconds
        except Exception as exc:
            # 不输出URL、数据库凭据或原始异常正文；失败不使API退出。
            logger.warning('news_worker retrying errorType=%s', type(exc).__name__)
        if not stop.is_set():
            await asyncio.to_thread(stop.wait, delay)


@asynccontextmanager
async def news_lifespan(_application):
    stop = Event()
    task = asyncio.create_task(news_loop(stop), name='news-collector')
    try:
        yield
    finally:
        stop.set()
        # 等待在途有超时边界的请求结束/事务落盘，再退出；绝不让旧任务后台遗留。
        await task
