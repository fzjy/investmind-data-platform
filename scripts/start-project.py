"""按项目配置启动本机服务或现有 Compose 部署。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/scheduled-tasks.json"
SCHEDULER_ENV = {
    "stocks": "DANG_SCHEDULER_DAILY_ENABLED",
    "announcements": "DANG_SCHEDULER_ANNOUNCEMENTS_ENABLED",
    "macro": "DANG_MACRO_SYNC_ENABLED",
    "market": "DANG_ROUTINE_MARKET_ENABLED",
    "marketPulse": "DANG_ROUTINE_MARKET_PULSE_ENABLED",
    "regions": "DANG_ROUTINE_REGION_ENABLED",
    "etfHeatmap": "DANG_ROUTINE_ETF_HEATMAP_ENABLED",
    "commodities": "DANG_ROUTINE_COMMODITIES_ENABLED",
}


def load_tasks(path: Path) -> dict[str, bool]:
    tasks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(tasks, dict) or tasks.keys() != {"news", *SCHEDULER_ENV}:
        raise ValueError("采集配置应包含任务 " + ", ".join(["news", *SCHEDULER_ENV]))
    for name, enabled in tasks.items():
        if type(enabled) is not bool:
            raise ValueError(f"{name} 必须为 true 或 false")
    return tasks


def services(tasks):
    selected = ["api"]
    if tasks["news"]:
        selected.append("news-worker")
    if any(tasks[name] for name in SCHEDULER_ENV):
        selected.append("scheduler")
    return selected


def scheduler_env(tasks):
    return {env: str(tasks[name]).lower() for name, env in SCHEDULER_ENV.items()}


def start_compose(tasks, files):
    command = ["docker", "compose", "-f", "docker-compose.backend.yml"]
    for path in files:
        command.extend(["-f", path])
    command.extend(["--profile", "scheduler"])
    env = {**os.environ, **scheduler_env(tasks)}
    selected = services(tasks)
    subprocess.run([*command, "config", "--quiet"], cwd=ROOT, env=env, check=True)
    disabled = [name for name in ("news-worker", "scheduler") if name not in selected]
    if disabled:
        subprocess.run([*command, "stop", *disabled], cwd=ROOT, env=env, check=True)
    existing = subprocess.run([*command, "ps", "-a", "-q", "api"], cwd=ROOT,
                              env=env, check=True, capture_output=True, text=True)
    if existing.stdout.strip():
        subprocess.run([*command, "up", "-d", "--wait", "postgres"], cwd=ROOT, env=env, check=True)
        subprocess.run([*command, "up", "-d", "--no-deps", *selected], cwd=ROOT, env=env, check=True)
    else:
        subprocess.run([*command, "up", "-d", *selected], cwd=ROOT, env=env, check=True)


async def serve_local(tasks, port):
    import uvicorn
    from app.main import create_app

    # 先占用监听端口，避免重复启动项目时先触发一轮采集。
    from app.local_admin.routes import mount_admin
    from app.local_admin.workers import LocalManager
    from app.market.data.catalog import DEFAULT_MARKET_CATALOG_FILE
    application = create_app()
    config = uvicorn.Config(application, host="127.0.0.1", port=port)
    server = uvicorn.Server(config)
    with config.bind_socket() as sock:
        manager = LocalManager(ROOT, market_path=DEFAULT_MARKET_CATALOG_FILE)
        mount_admin(application, project_root=ROOT, manager=manager)
        watcher = None
        try:
            manager.start()
            async def watch_manager():
                try:
                    await manager.monitor()
                finally:
                    server.should_exit = True
            watcher = asyncio.create_task(watch_manager())
            await server.serve(sockets=[sock])
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            manager.shutdown()
            if watcher and not watcher.cancelled():
                watcher.result()


def run_child(name):
    # 父进程退出（含终端被关闭）时管道关闭，子进程收到与 Ctrl+C 相同的停止请求。
    import threading
    owner = os.environ.get("INVESTMIND_LOCAL_CONTROL")
    parent_closed = threading.Event()

    def stop_on_eof():
        os.read(sys.stdin.fileno(), 1)
        if owner:
            parent_closed.set()
            return
        if os.name == "nt":
            # Windows 控制台事件能唤醒正在 sleep/wait 的主线程。
            try:
                os.kill(os.getpid(), signal.CTRL_BREAK_EVENT)
            except OSError:
                import _thread
                _thread.interrupt_main()
        else:
            os.kill(os.getpid(), signal.SIGINT)

    if os.name == "nt":
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    threading.Thread(target=stop_on_eof, daemon=True).start()
    control = None
    if owner:
        from app.local_admin.control import ControlStore
        from app.config import Settings
        from app.db.session import create_engine_and_session
        _, factory = create_engine_and_session(Settings.from_env().database_url)
        control = ControlStore(factory, owner)
    if name == "scheduler":
        from app.jobs.scheduler import main as run_scheduler
        return run_scheduler(control_store=control, stop_requested=parent_closed.is_set)
    if control is not None:
        from app.local_admin.workers import managed_news
        return managed_news(control, stop_requested=parent_closed.is_set)
    from app.cli.main import app
    app(args=["watch-news"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", action="store_true", help="启动 Compose 服务")
    parser.add_argument("--compose-file", action="append", default=[], help="追加 Compose 覆盖文件")
    parser.add_argument("--plan", action="store_true", help="只输出任务和服务清单")
    args = parser.parse_args()
    tasks = load_tasks(CONFIG)
    print(json.dumps({"tasks": tasks, "services": services(tasks)}, ensure_ascii=False), flush=True)
    if args.plan:
        return
    if args.compose:
        start_compose(tasks, args.compose_file)
    else:
        from dotenv import dotenv_values
        port = os.environ.get("DANG_BACKEND_PORT") or dotenv_values(ROOT / ".env").get("DANG_BACKEND_PORT") or 8000
        asyncio.run(serve_local(tasks, int(port)))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child" and sys.argv[2] in {"news-worker", "scheduler"}:
        sys.exit(run_child(sys.argv[2]))
    try:
        main()
    except KeyboardInterrupt:
        pass
