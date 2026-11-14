"""从同一只读事务采集选股路由的分页结果指纹，仅输出公开字段摘要。"""

import hashlib
import json
from math import ceil
from pathlib import Path
import sys
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.models import BoardId
from app.stocks.routes import create_router


def measure(client, metrics, enabled, page_size, path):
    request = {
        "filters": {"preset": None, "thresholds": {"dividendYield": 0}, "enabledThresholds": enabled,
                    "exclusions": [], "boards": list(get_args(BoardId)),
                    "categories": {"majorIndustry": [], "subIndustry": [], "concept": [], "region": []}},
        "sortMode": "quick", "sortRules": [{"field": "dividendYield", "direction": "desc", "enabled": True}],
        "pageSize": page_size,
    }
    rows = []
    pages = 1
    page = 1
    start = metrics["sql"]
    snapshot = None
    fields = ("dividendYield", "expectedDividendYield", "expectedDividendYieldNTM")
    while page <= pages:
        response = client.post(path, json={**request, "page": page})
        response.raise_for_status()
        data = response.json()["data"]
        if page == 1:
            snapshot = data["snapshot"]
            total = data["total"]
            pages = max(1, ceil(total / request["pageSize"]))
        assert data["snapshot"] == snapshot and data["total"] == total and data["page"] == page
        rows.extend([stock["symbol"], *(stock[field] for field in fields)] for stock in data["items"])
        page += 1
    assert len(rows) == len({row[0] for row in rows}) == total
    observed = [row[1] for row in rows if row[1] is not None]
    assert observed == sorted(observed, reverse=True)
    if enabled:
        assert len(observed) == total and all(value >= 0 for value in observed)
    return {"request": request, "snapshot": snapshot, "rows": total, "http": pages, "sql": metrics["sql"] - start,
            "nulls": {field: sum(row[i] is None for row in rows) for i, field in enumerate(fields, 1)},
            "orderedRowsSha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}


def main():
    with open_readonly_factory(settings.database_read_url) as factory:
        metrics = {"sql": 0}

        def count(*args):
            metrics["sql"] += 1

        event.listen(factory.kw["bind"], "before_cursor_execute", count)
        api = FastAPI()
        api.include_router(create_router(provider=StocksDatabaseProvider(SnapshotRepository(factory), factory)))
        with TestClient(api) as client:
            paths = ["/api/data/screener", "/api/v1/stocks/screener"]
            reports = {}
            for path in paths:
                reports[path] = [measure(client, metrics, enabled, size, path)
                                 for enabled, size in (([], 200), (["dividendYield"], 200), (["dividendYield"], 10))]
                assert reports[path][1]["orderedRowsSha256"] == reports[path][2]["orderedRowsSha256"]
            assert reports[paths[0]] == reports[paths[1]]
            print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
