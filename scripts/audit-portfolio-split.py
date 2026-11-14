"""真实公开快照 + 指定验证样本。通过私有 HTTP 服务，核对底座实际收到的请求。"""

from argparse import ArgumentParser
from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import socket
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from app.config import settings
from app.portfolio.routes import create_router
from app.portfolio.database_provider import PortfolioDatabaseProvider
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository


@contextmanager
def running(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:" + str(server.server_port)
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def private_service(website, public_url):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    folder = website / "website-services/portfolio-analysis"
    with subprocess.Popen([sys.executable, "server.py"], cwd=folder,
            env={**os.environ, "PORTFOLIO_ANALYSIS_PORT":str(port), "PORTFOLIO_ANALYSIS_HOST":"127.0.0.1",
                 "BACKEND_INTERNAL_API_BASE_URL":public_url}, stdout=subprocess.DEVNULL) as process:
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("网站私有组合服务未能启动")
                try:
                    with socket.create_connection(("127.0.0.1",port),.25):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError("等待网站私有组合服务超时")
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            process.wait(timeout=10)


def audit(factory, website):
    app = FastAPI()
    app.include_router(
        create_router(provider=PortfolioDatabaseProvider(SnapshotRepository(factory)))
    )
    client = TestClient(app)
    sql = []
    event.listen(factory.kw["bind"], "before_cursor_execute", lambda _, __, statement, *args: sql.append(statement))
    new = client.get("/api/v1/portfolio")
    before = len(sql)
    old = client.get("/api/data/portfolio")
    assert new.status_code == old.status_code == 200 and before == len(sql) - before == 1
    response = new.json()
    raw = response["data"]
    legacy = old.json()
    previous_template = legacy["data"].pop("defaultState")
    previous_key = legacy["data"].pop("storageKey")
    assert not previous_template["positions"] and legacy == response
    for path in ("/api/data/portfolio", "/api/v1/portfolio"):
        assert client.post(path, json={"holdings": []}).status_code == 405
        assert client.post(path + "/analysis", json={"holdings": []}).status_code == 404
    observed = []

    class PublicTransport(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/v1/portfolio" and not self.headers.get(
                "Content-Length"
            )
            assert not any(
                k.lower() in ("authorization", "cookie", "x-test-private")
                for k in self.headers
            )
            n = len(sql)
            validator = self.headers.get("If-None-Match")
            result = client.get(self.path, headers={"If-None-Match": validator} if validator else {})
            observed.append(
                {
                    "method": "GET",
                    "path": self.path,
                    "sql": len(sql) - n,
                    "bodyBytes": 0,
                    "responseStatus": result.status_code,
                    "responseBytes": len(result.content),
                    "selectedPayload": any(".payload" in statement for statement in sql[n:]),
                }
            )
            self.send_response(result.status_code)
            self.send_header("ETag",result.headers["etag"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result.content)))
            self.end_headers()
            self.wfile.write(result.content)

        def log_message(self, *args):
            pass

    asof = date.fromisoformat(raw["snapshotLabel"])
    start = (asof - timedelta(days=365)).isoformat()
    sample = [
        {"assetId": "600519.SH", "weight": 60},
        {"assetId": "000001.SZ", "weight": 30},
        {"assetId": "CNY", "weight": 10},
    ]
    bench = sorted(raw.get("benchmarks", {}))[0]
    cases = [
        ("整段发布历史", sample, None, None),
        ("一年窗口", sample, start, str(asof)),
        ("纯现金", [{"assetId": "CNY", "weight": 100}], start, str(asof)),
        (
            "业务日后空窗口",
            sample,
            str(asof + timedelta(days=1)),
            str(asof + timedelta(days=2)),
        ),
    ]
    projection = []
    responses = []
    with running(PublicTransport) as host, private_service(website, host) as site:
        for name, holdings, left, right in cases:
            body = {"holdings": holdings, "benchmarkAssetId": bench}
            if left:
                body.update(startDate=left, endDate=right)
            req = Request(
                site + "/analysis",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Test-Private": "audit-only",
                },
            )
            with urlopen(req, timeout=60) as res:
                assert res.status == 200
                assert res.headers["Cache-Control"] == "no-store"
                data = json.load(res)
            assert data["meta"] == response["meta"]
            p = data["data"]
            projection.append(dict(name=name, **body, privateNav=p["nav"]))
            responses.append(
                dict(
                    name=name,
                    window=p["effectiveWindow"],
                    metrics=p["metrics"],
                    capabilities={k: v["status"] for k, v in p["capabilities"].items()},
                    warnings=p["warnings"],
                )
            )
        rejected = Request(
            site + "/analysis",
            data=json.dumps({"holdings": sample, "trades": []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(rejected, timeout=10).close()
            raise AssertionError("意外接受了交易记录")
        except HTTPError as exc:
            assert exc.code == 422
    assert len(cases) == len(observed) and all(q["sql"] == 1 for q in observed)
    assert [q["responseStatus"] for q in observed] == [200] + [304] * (len(cases)-1)
    assert [q["selectedPayload"] for q in observed] == [True] + [False] * (len(cases)-1)
    website_data = {**raw, "sourceMeta": response["meta"]}
    js = subprocess.run(
        ["node", "scripts/audit-portfolio-projection.mjs"],
        cwd=website,
        input=json.dumps({"data": website_data, "cases": projection, "previousTemplate": previous_template, "previousKey": previous_key}),
        stdout=subprocess.PIPE,
        encoding="utf-8",
        check=True,
    )
    return dict(
        meta=response["meta"],
        publicFactsAndMetaMatch=True,
        sqlPerAlias=1,
        legacyTemplatePositions=len(previous_template["positions"]),
        websiteTemplateMatchesLegacy=True,
        publicFields=sorted(raw),
        missingData=raw.get("missingData"),
        assets=dict(Counter(a["type"] for a in raw["assets"])),
        priceDates={
            k: {"assets": len(raw[k]), "points": sum(len(p) for p in raw[k].values())}
            for k in ("prices", "unadjustedPrices")
        },
        benchmarks={k: v.get("coverage") for k, v in raw.get("benchmarks", {}).items()},
        ledgerAssets=len(raw.get("dividendLedger", {}).get("assets", {})),
        yieldAssets=len(raw.get("dividendYieldInputs", {})),
        attributionAssets=len(raw.get("attributionInputs", {}).get("assets", {})),
        factorCoverageAssets=len(raw.get("factorCoverage", {})),
        publicSnapshotDigest=hashlib.sha256(new.content).hexdigest(),
        privateServiceReads=observed,
        rejectedPrivateFields={"trades": 422, "publicRequests": 0},
        privateResults=responses,
        websiteResults=json.loads(js.stdout),
    )


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--website", type=Path, required=True, help="含网站私有服务和投影驱动的仓目录")
    args = parser.parse_args()
    with open_readonly_factory(
        settings.database_read_url
    ) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        result = audit(factory, args.website.resolve())
        result["historicalTemplates"] = [
            dict(row)
            for row in session.execute(text("""
                SELECT count(*) AS versions, payload->>'storageKey' AS key,
                       (payload->'defaultState')::jsonb - 'positions' AS template,
                       jsonb_array_length((payload->'defaultState'->'positions')::jsonb) AS positions
                FROM mart.page_snapshot
                WHERE product='portfolio'
                GROUP BY 2, 3, 4
                ORDER BY versions DESC
            """)).mappings()
        ]
    data = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        args.output.write_text(data + "\n", encoding="utf-8")
    else:
        print(data)
