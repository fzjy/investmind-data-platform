"""本机父进程协调目标配置；工作进程以自己的确认作为生效依据。"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import RLock

from app.config import Settings
from app.local_admin.configuration import Configuration, MARKET_TASKS, TASKS
from app.local_admin.control import ControlStore
from app.market.data.catalog import MarketDataCatalog


class LocalManager:
    def __init__(self, root, launch=None):
        self.root = root
        self.settings = Settings.from_env()
        self.config = Configuration(root, news_path=self.settings.news_config_path)
        self.store = ControlStore(root / "data/local-admin/control.sqlite3")
        self.launch = launch or self._spawn
        self.workers = {}
        self.failed = {}
        self.lock = RLock()

    def _spawn(self, name):
        return subprocess.Popen([sys.executable, str(self.root / "scripts/start-project.py"), "--child", name],
            cwd=self.root, env={**os.environ, "INVESTMIND_LOCAL_CONTROL": str(self.store.path)},
            stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt")

    def sync_files(self):
        enabled = self.config.read("tasks")["fields"]
        market = MarketDataCatalog.load(self.config.paths["market"]).schedule
        interval = self.config.read("news")["fields"]["interval_seconds"]
        s = self.settings
        targets = []
        for name in TASKS:
            if name == "news":
                rule = f"每 {interval} 秒"
            elif name == "macro":
                day = dict(mon="一", tue="二", wed="三", thu="四", fri="五", sat="六", sun="日")[s.macro_sync_day_of_week]
                rule = f"每周{day} {s.macro_sync_time:%H:%M} · {s.sync_timezone}"
            elif name in MARKET_TASKS:
                rule = f"每天 {market.time:%H:%M} · {market.timezone}"
            else:
                rule = f"每天 {s.sync_hour:02}:{s.sync_minute:02} · {s.sync_timezone}"
            targets.append((name, enabled[name], rule))
        self.store.targets(targets)

    def start(self):
        with self.lock:
            self.sync_files()
            self.store.reset_instance(TASKS, cold=True)
            self.reconcile()

    def apply(self, name, version, changes):
        with self.lock:
            saved = self.config.save(name, version, changes)
            self.sync_files()
            self.failed.clear()
            self.reconcile()
            return saved

    def reconcile(self):
        with self.lock:
            rows = self.store.rows()
            for worker, names in (("news-worker", ["news"]), ("scheduler", [n for n in TASKS if n != "news"])):
                process = self.workers.get(worker)
                if process and process.poll() is not None:
                    if process.returncode:
                        self.failed[worker] = f"进程退出（{process.returncode}），可重新应用"
                    self.store.reset_instance(names)
                    process.stdin.close()
                    del self.workers[worker]
                    process = None
                wanted = any(rows[n]["enabled"] for n in names)
                if not process and wanted and worker not in self.failed:
                    try:
                        self.workers[worker] = self.launch(worker)
                    except OSError as error:
                        self.failed[worker] = f"启动失败（{type(error).__name__}）"
                elif worker == "scheduler" and process and not wanted and not process.stdin.closed and all(
                    not rows[n]["active"] and rows[n]["applied"] == rows[n]["generation"] for n in names
                ):
                    process.stdin.close()
                elif not process:
                    for n in names:
                        row = self.store.get(n)
                        if row["enabled"] or row["applied"] != row["generation"] or row["rule"] != row["effective"]:
                            self.store.acknowledge(row, error=self.failed.get(worker))

    async def monitor(self):
        while True:
            await asyncio.sleep(1)
            await asyncio.to_thread(self.reconcile)

    def shutdown(self):
        for worker in self.workers.values():
            worker.stdin.close()
        for worker in self.workers.values():
            worker.wait()


def managed_news(store, *, runner=None, settings_loader=Settings.from_env, sleep=time.sleep):
    from smartparse_news import Config
    from app.content.news_runtime import run_news
    from app.jobs.scheduler_control import summarize
    from datetime import UTC, datetime, timedelta
    due = 0
    last_generation = None
    while True:
        row = store.get("news")
        if not row["enabled"]:
            store.acknowledge(row)
            return 0
        generation = row["generation"]
        if last_generation != generation:
            due = 0
        last_generation = generation
        if time.monotonic() < due:
            sleep(min(0.5, due - time.monotonic()))
            continue
        settings = settings_loader()
        config = Config.read(settings.news_config_path)
        if not config.enabled:
            store.acknowledge(row, error="新闻来源配置已关闭")
            return 1
        applied = {**row, "rule": f"每 {config.interval_seconds} 秒"}
        store.acknowledge(applied)
        recovery = generation != row["recovered"]
        if store.admit("news", generation, recovery=recovery):
            try:
                report = (runner or run_news)(settings, publish=True)
                store.finish("news", summarize("news", report, recovery))
            except Exception:
                store.finish("news", "失败，请查看新闻运行记录")
                raise
        due = time.monotonic() + config.interval_seconds
        when = datetime.now(UTC) + timedelta(seconds=config.interval_seconds)
        store.acknowledge(applied, when.isoformat())
