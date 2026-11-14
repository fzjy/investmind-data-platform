"""通过应用入口核对主要公共产品，保留内容指纹与缺口状态。"""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient
from app.main import create_app


def probe():
    endpoints = [
        ("GET", "/health"), ("GET", "/api/data/market-overview"),
        ("GET", "/api/data/market-explorer"), ("GET", "/api/data/market-news"),
        ("GET", "/api/data/macro-transmission"), ("GET", "/api/data/macro-insights"),
        ("GET", "/api/data/portfolio"), ("GET", "/api/data/search?q=600519"),
        ("GET", "/api/data/stocks/600519.SH"), ("POST", "/api/data/stocks/research"),
    ]
    reports = []
    with TestClient(create_app(background_news=False)) as client:
        for method, path in endpoints:
            response = client.request(method, path, **({"json": {"symbol": "600519.SH"}} if method == "POST" else {}))
            payload = response.json()
            # 比较发布产品数据。搜索的随机请求编号与宏观运维检查时间独立于数据版本。
            data = payload.get("data", payload.get("error", payload))
            content = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            reports.append({"method": method, "path": path, "status": response.status_code,
                            "version": payload.get("meta", {}).get("version"),
                            "error": payload.get("error", {}).get("code"),
                            "dataSha256": hashlib.sha256(content).hexdigest()})
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    probe()
