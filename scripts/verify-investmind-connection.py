"""通过运行中的网站代理和底座比对公共产品，只输出摘要。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
from time import perf_counter, sleep
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError
from urllib.parse import urlencode
from unittest.mock import patch
from uuid import UUID

import simplejson


FILTERS = dict(preset=None, thresholds={}, enabledThresholds=[], exclusions=[],
               boards=["sh-main", "sz-main", "star", "chinext", "bse"],
               categories=dict(majorIndustry=[], subIndustry=[], concept=[], region=[]))
REQUESTS = {
    "market/overview": None, "market/explorer": None,
    "macro/transmission": None, "macro/insights": None, "portfolio": None,
    "stocks/600519.SH": None, "stocks/research": {"symbol": "600519.SH"},
    "stocks/screener": dict(filters=FILTERS, sortRules=[], page=1, pageSize=5),
    "search?q=600519": None, "news?limit=3": None,
}


def fetch(opener, url, body):
    req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json", "Accept": "application/json"})
    start = perf_counter()
    with opener.open(req, timeout=45) as response:
        payload = simplejson.load(response, use_decimal=True)
    duration = round((perf_counter() - start) * 1000)
    dynamic = {}
    if "/v1/search?" in url:
        dynamic["requestId"] = str(UUID(payload["meta"].pop("requestId")))
        if payload["data"]["stocks"]["status"] != "ready" or not payload["data"]["stocks"]["items"]:
            raise ValueError("样本证券搜索未返回已发布匹配项")
    if url.endswith("/macro/insights"):
        audit = payload["collectionAudit"]
        stamp = datetime.fromisoformat(audit.pop("readAt"))
        if stamp.utcoffset() is None:
            raise ValueError("采集状态读取时间缺少时区")
        dynamic["readAt"] = stamp.isoformat()
    encoded = simplejson.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                               use_decimal=True).encode()
    meta = payload.get("meta", {})
    return dict(sha256=sha256(encoded).hexdigest(), bytes=len(encoded), ms=duration,
                version=meta.get("version"), source=meta.get("source"),
                dynamic=dynamic, has_data=bool(payload.get("data") or payload.get("items")))


def compare(backend_port, website_port):
    backend = f"http://127.0.0.1:{backend_port}/api/v1/"
    website = f"http://127.0.0.1:{website_port}/backend-api/api/v1/"
    opener = build_opener(ProxyHandler({}))
    results = {}
    for path, body in REQUESTS.items():
        try:
            direct = fetch(opener, backend + path, body)
            via_site = fetch(opener, website + path, body)
            results[path] = dict(direct=direct, website=via_site,
                                 equal=direct["sha256"] == via_site["sha256"])
            if not (direct["has_data"] and via_site["has_data"]):
                raise ValueError("样本产品应返回非空业务数据")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            results[path] = dict(error=str(exc))
    return results


def catalog_checks(port):
    """从管理台为每个条目登记的示例调用候选接口，复查游标结构与缓存。"""
    root = Path(__file__).resolve().parents[1]
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    rows = {}
    for file in sorted((root / "docs/data-catalog").glob("*.json")):
        for entry in json.loads(file.read_text(encoding="utf-8")):
            for operation in entry["access"]:
                path = operation.get("path", "")
                if not path.startswith("/api/v1/datasets/"):
                    continue
                target = path + "?" + urlencode(operation["example"])
                if target in rows:
                    continue
                with opener.open(base + target, timeout=45) as response:
                    page = json.load(response)
                if page["meta"]["dataset"] != path.rsplit("/", 1)[1] or not isinstance(page["data"], list):
                    raise ValueError(f"数据集示例响应异常 {path}")
                rows[target] = len(page["data"])
    with opener.open(base + "/openapi.json", timeout=15) as response:
        paths = {p for p in json.load(response)["paths"] if p.startswith("/api/v1/datasets/")}
    assert {url.split("?")[0] for url in rows} == paths
    with opener.open(base + "/api/v1/portfolio", timeout=30) as response:
        etag = response.headers["ETag"]
    request = Request(base + "/api/v1/portfolio", headers={"If-None-Match":etag})
    try:
        with opener.open(request, timeout=30):
            raise ValueError("组合条件请求未返回 304")
    except HTTPError as exc:
        if exc.code != 304 or exc.headers.get("ETag") != etag or exc.read():
            raise
    return dict(routes=len(paths), examples=rows, portfolioConditionalStatus=304)


@contextmanager
def shadow(website):
    """用工作区真实应用和同一网站的生产构建在临时端口演练切换。"""
    import uvicorn
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.db.session import create_engine_and_session
    from app.main import create_app
    if not (website / ".next/BUILD_ID").is_file():
        raise ValueError("指定的网站缺少生产构建")
    def readonly(url):
        return create_engine_and_session(url, connection_options={"options": "-c default_transaction_read_only=on"})
    with patch("app.main.create_engine_and_session", readonly):
        application = create_app(background_news=False)
    with socket.socket() as api_sock, socket.socket() as site_sock:
        api_sock.bind(("127.0.0.1", 0))
        site_sock.bind(("127.0.0.1", 0))
        api_port, site_port = api_sock.getsockname()[1], site_sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets":[api_sock]}, daemon=True)
        thread.start()
        site_sock.close()
        try:
            env = {**os.environ, "BACKEND_INTERNAL_API_BASE_URL": f"http://127.0.0.1:{api_port}",
                   "NEXT_TELEMETRY_DISABLED":"1", "HOSTNAME":"127.0.0.1", "PORT":str(site_port)}
            entry = website / ".next/standalone/server.js"
            command = ["node", str(entry)] if entry.is_file() else [
                "node", "node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1", "-p", str(site_port)]
            site = subprocess.Popen(command, cwd=website, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL)
            try:
                for _ in range(200):
                    if site.poll() is not None:
                        raise RuntimeError("临时网站启动失败")
                    try:
                        with socket.create_connection(("127.0.0.1", site_port), timeout=.25):
                            if server.started:
                                break
                    except OSError:
                        pass
                    sleep(.1)
                else:
                    raise RuntimeError("临时网站或底座未就绪")
                yield api_port, site_port
            finally:
                site.terminate()
                site.wait(timeout=15)
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            application.state.read_engine.dispose()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend-port", type=int, required=True)
    p.add_argument("--website-port", type=int, required=True)
    p.add_argument("--shadow-website", type=Path, help="已构建的网站仓库，在两个临时端口验证当前工作区")
    args = p.parse_args()
    results = compare(args.backend_port, args.website_port)
    report = dict(checked_at=datetime.now(timezone.utc).isoformat(), products=results)
    if args.shadow_website:
        with shadow(args.shadow_website.resolve()) as (api_port, site_port):
            candidate = compare(api_port, site_port)
            report["candidate"] = candidate
            report["datasets"] = catalog_checks(api_port)
            report["same_products"] = {k: bool(v.get("equal") and candidate[k].get("equal")
                and v["direct"]["sha256"] == candidate[k]["direct"]["sha256"]) for k,v in results.items()}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return int(any(not row.get("equal") for row in results.values())
               or not all(report.get("same_products", {}).values()))


if __name__ == "__main__":
    raise SystemExit(main())
