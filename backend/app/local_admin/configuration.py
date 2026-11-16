from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from smartparse_news import Config as NewsConfig
from app.market.data.catalog import MarketDataCatalog


# 启动器的 Compose 模式仅依赖标准库；其契约测试核对两处开关一致。
TASKS = {
    "stocks": ("证券日同步", "按股票池与交易日历同步数据", "sync-daily"),
    "announcements": ("证券公告", "按公告窗口分页采集并发布", "sync-announcements"),
    "macro": ("宏观数据", "按周槽位采集并发布宏观系列", "sync-macro"),
    "news": ("财经快讯", "按轮询间隔采集并交付新闻", "watch-news"),
    "market": ("基础行情", "更新市场指数与行情快照", "sync-market-data"),
    "marketPulse": ("市场脉搏", "更新市场脉搏指标", "sync-market-pulse"),
    "regions": ("地域数据", "刷新证券地域信息", "sync-security-region"),
    "etfHeatmap": ("ETF 热力图", "更新 ETF 热力图数据", "sync-etf-heatmap"),
    "commodities": ("商品行情", "更新商品报价与历史", "sync-commodities"),
}
MARKET_TASKS = ("market", "marketPulse", "regions", "etfHeatmap", "commodities")


class Conflict(ValueError):
    pass


class Configuration:
    def __init__(self, root: Path, *, news_path: Path | None = None, market_path: Path | None = None):
        self.paths = {
            "tasks": root / "config/scheduled-tasks.json",
            "market": market_path or root / "config/market/market-data.json",
            "news": news_path or root / "config/news/news-collector.json",
        }
        self.lock = RLock()

    def _fields(self, name, document):
        if name == "tasks":
            if document.keys() != TASKS.keys() or any(type(v) is not bool for v in document.values()):
                raise ValueError("任务开关须为九项布尔值")
            return document.copy()
        if name == "market":
            catalog = MarketDataCatalog.model_validate(document)
            return {"time": catalog.schedule.time.strftime("%H:%M")}
        config = NewsConfig(**document)
        return {"interval_seconds": config.interval_seconds}

    def read(self, name):
        raw = self.paths[name].read_bytes()
        return {"version": hashlib.sha256(raw).hexdigest(),
                "fields": self._fields(name, json.loads(raw))}

    def save(self, name, expected_version, changes):
        path = self.paths[name]
        with self.lock:
            original = path.read_bytes()
            if hashlib.sha256(original).hexdigest() != expected_version:
                raise Conflict("配置已被修改，请重新载入")
            document = json.loads(original)
            current = self._fields(name, document)
            if not changes.keys() <= current.keys():
                raise ValueError("不支持该配置字段")
            section = document["schedule"] if name == "market" else document
            section.update(changes)
            validated = self._fields(name, document)
            if validated == current:
                return self.read(name)
            raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
            with NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                temporary = Path(tmp.name)
                try:
                    tmp.write(raw)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                except BaseException:
                    tmp.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                if path.read_bytes() != original:
                    raise Conflict("配置已被修改，请重新载入")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"version": hashlib.sha256(raw).hexdigest(), "fields": validated}
