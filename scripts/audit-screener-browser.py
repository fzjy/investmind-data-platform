"""用现有生产构建和只读开发库核验浏览器取数。运行前安装 Playwright 并设置浏览器路径。"""

from argparse import ArgumentParser
import asyncio
import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import event
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.routes import create_router
from app.portfolio.database_provider import PortfolioDatabaseProvider
from app.portfolio.routes import create_router as portfolio_router
from app.market.database_provider import MarketDatabaseProvider
from app.market.routes import create_router as market_router
from app.content.routes import create_router as news_router, read_news


def run(website, driver, output):
    # 同一数据库事务和一套真实选股路由。审计服务不导入采集生命周期。
    with open_readonly_factory(settings.database_read_url) as factory, socket.socket() as listener:
        # 本机生产构建可能把公开 API 的端口编入客户端，冲突时直接失败。
        listener.bind(("127.0.0.1", int(os.environ.get("AUDIT_PUBLIC_API_PORT", "0"))))
        api_port = listener.getsockname()[1]
        calls = []
        context = ContextVar("screener_sql", default=None)

        def count(*args):
            current = context.get()
            if current is not None:
                current.append(1)

        event.listen(factory.kw["bind"], "before_cursor_execute", count)
        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://127\.0\.0\.1:\d+",
                           allow_methods=["POST", "GET"], allow_headers=["content-type"])
        app.include_router(create_router(provider=StocksDatabaseProvider(SnapshotRepository(factory), factory)))
        app.include_router(portfolio_router(provider=PortfolioDatabaseProvider(SnapshotRepository(factory))))
        if driver == "audit-market-browser.mjs":
            app.include_router(market_router(provider=MarketDatabaseProvider(SnapshotRepository(factory)),
                                            latest_news=lambda: read_news(factory)))
            app.include_router(news_router(factory))

        @app.middleware("http")
        async def capture(request, call_next):
            if request.url.path.startswith("/audit/") or request.method == "OPTIONS":
                return await call_next(request)
            query = await request.json() if request.method == "POST" else {}
            counter = []
            token = context.set(counter)
            try:
                response = await call_next(request)
            finally:
                context.reset(token)
            calls.append({"sql": len(counter), "path": request.url.path, "status": response.status_code,
                          "method": request.method, "page": query.get("page"), "pageSize": query.get("pageSize"),
                          "thresholds": {k: query["filters"]["thresholds"][k] for k in query.get("filters", {}).get("enabledThresholds", [])},
                          "querySha256": hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()})
            return response

        @app.get("/audit/calls")
        def completed_calls():
            return calls

        server = uvicorn.Server(uvicorn.Config(app, log_level="error", loop="asyncio"))
        thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
        thread.start()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            site_port = available.getsockname()[1]
        host = f"http://127.0.0.1:{api_port}"
        env = {**os.environ, "BACKEND_INTERNAL_API_BASE_URL": host, "NEXT_TELEMETRY_DISABLED": "1"}
        site = subprocess.Popen(["node", "node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1",
                                 "-p", str(site_port)], cwd=website, env=env, stdout=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", site_port), .25):
                        break
                except OSError:
                    if site.poll() is not None:
                        raise RuntimeError("网站未能启动")
                    time.sleep(.1)
            subprocess.run(["node", f"scripts/{driver}", f"http://127.0.0.1:{site_port}",
                            host, str(output)], check=True, env=env, cwd=website)
        finally:
            site.terminate()
            site.wait(timeout=10)
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--website", type=Path, required=True, help="已构建的 InvestMind 网站仓目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--market", action="store_true")
    mode.add_argument("--workbench", action="store_true")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    website = args.website.resolve()
    driver = "audit-market-browser.mjs" if args.market else "audit-workbench-browser.mjs" if args.workbench else "audit-screener-browser.mjs"
    if not (website / ".next/BUILD_ID").is_file() or not (website / "scripts" / driver).is_file():
        parser.error("指定网站缺少生产构建或对应浏览器驱动")
    run(website, driver, args.output.resolve())
