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
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port)
    server = uvicorn.Server(config)
    workers = {}
    with config.bind_socket() as sock:
        try:
            for name in services(tasks):
                if name == "api":
                    continue
                workers[name] = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--child", name],
                    cwd=ROOT, env={**os.environ, **scheduler_env(tasks)},
                    stdin=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )

            async def monitor():
                while workers:
                    for name, worker in workers.items():
                        if worker.poll() is not None:
                            server.should_exit = True
                            raise RuntimeError(f"{name} 提前退出，退出码 {worker.returncode}")
                    await asyncio.sleep(0.5)

            watcher = asyncio.create_task(monitor())
            try:
                await server.serve(sockets=[sock])
                if watcher.done():
                    watcher.result()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        finally:
            for worker in workers.values():
                worker.stdin.close()
            for worker in workers.values():
                worker.wait()


def run_child(name):
    # 父进程退出（含终端被关闭）时管道关闭，子进程收到与 Ctrl+C 相同的停止请求。
    import threading

    def stop_on_eof():
        os.read(sys.stdin.fileno(), 1)
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
    if name == "scheduler":
        from app.jobs.scheduler import main as run_scheduler
        return run_scheduler()
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
