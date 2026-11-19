"""以相同只读事务重放已发布产品的公开与兼容路由，保存可比较的响应指纹。"""

from datetime import datetime
from hashlib import sha256
import json
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
import app.main as main


ALIASES = {
    "/api/data/market-overview": "/api/v1/market/overview",
    "/api/data/market-explorer": "/api/v1/market/explorer",
    "/api/data/macro-transmission": "/api/v1/macro/transmission",
    "/api/data/macro-insights": "/api/v1/macro/insights",
    "/api/data/portfolio": "/api/v1/portfolio",
    "/api/data/market-news": "/api/v1/news",
    "/api/data/stocks/research": "/api/v1/stocks/research",
    "/api/data/screener": "/api/v1/stocks/screener",
    "/api/data/stocks/600519.SH": "/api/v1/stocks/600519.SH",
    "/api/data/search": "/api/v1/search",
}


def digest(obj):
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return sha256(body).hexdigest()


def normalize_dynamic(path, obj, request_ids):
    public_path = ALIASES.get(path, path)
    if public_path == "/api/v1/search":
        request_id = obj["meta"].pop("requestId")
        parsed = UUID(request_id)
        if parsed.version != 4 or str(parsed) != request_id or request_id in request_ids:
            raise ValueError(f"{path} 的请求标识并非独立的规范 UUIDv4")
        request_ids.add(request_id)
        return ["meta.requestId"]
    if public_path not in ("/api/v1/macro/insights", "/api/v1/data-status"):
        return []
    scope, label = (
        (obj["collectionAudit"], "collectionAudit.readAt")
        if public_path.endswith("/insights")
        else (obj, "readAt")
    )
    stamp = scope.pop("readAt")
    if not isinstance(stamp, str) or "T" not in stamp or datetime.fromisoformat(stamp).utcoffset() is None:
        raise ValueError(f"{path} 的 {label} 需要带时区的 ISO 8601 时间")
    return [label]


def capture(client, method, path, params, request_ids):
    response = client.request(
        method, path, params=params if method == "GET" else None, json=params if method == "POST" else None
    )
    response.raise_for_status()
    obj = response.json()
    ignored = normalize_dynamic(path, obj, request_ids)
    full_digest = digest(obj)
    common_digest = full_digest
    legacy_only = []
    if path == "/api/data/portfolio":
        data = obj["data"]
        template = data.pop("defaultState")
        storage_key = data.pop("storageKey")
        if template["positions"] != [] or not isinstance(storage_key, str) or not storage_key:
            raise ValueError("组合旧接口的默认模板或存储标识不符合兼容合同")
        legacy_only = ["data.defaultState", "data.storageKey"]
        common_digest = digest(obj)
    row = {
        "method": method,
        "path": path,
        "parameters": params,
        "status": response.status_code,
        "stableJsonSha256": full_digest,
        "ignored": ignored,
        "meta": obj.get("meta"),
        "etag": response.headers.get("etag"),
    }
    if legacy_only:
        row["compatibilityOnlyFields"] = legacy_only
    return row, common_digest


def freeze(factory, read):
    screen = read("docs/research/investmind-screener-baseline.json")[0]["request"] | {"page": 1}
    calls = [
        ("GET", "/api/v1/market/overview", {}),
        ("GET", "/api/data/market-overview", {}),
        ("GET", "/api/v1/market/explorer", {}),
        ("GET", "/api/data/market-explorer", {}),
        ("GET", "/api/v1/market/explorer", {"news": "latest"}),
        ("GET", "/api/data/market-explorer", {"news": "latest"}),
        ("GET", "/api/v1/macro/transmission", {}),
        ("GET", "/api/data/macro-transmission", {}),
        ("GET", "/api/v1/macro/insights", {}),
        ("GET", "/api/data/macro-insights", {}),
        ("GET", "/api/v1/portfolio", {}),
        ("GET", "/api/data/portfolio", {}),
        ("GET", "/api/v1/news", {"limit": 20}),
        ("GET", "/api/data/market-news", {"limit": 20}),
        ("POST", "/api/v1/stocks/research", {"symbol": "600519.SH"}),
        ("POST", "/api/data/stocks/research", {"symbol": "600519.SH"}),
        ("POST", "/api/v1/stocks/screener", screen),
        ("POST", "/api/data/screener", screen),
        ("GET", "/api/v1/stocks/600519.SH", {}),
        ("GET", "/api/data/stocks/600519.SH", {}),
        ("GET", "/api/v1/search", {"q": "600519"}),
        ("GET", "/api/data/search", {"q": "600519"}),
        (
            "GET",
            "/api/v1/data-status",
            {"entryId": "stock-dividend", "subjectKey": "600519.SH", "limit": "1"},
        ),
    ]
    output = []
    with patch.object(main, "create_engine_and_session", return_value=(factory.kw["bind"].engine, factory)):
        app = main.create_app(background_news=False)
    digests_by_request = {}
    request_ids = set()
    with TestClient(app) as client:
        for method, path, params in calls:
            row, common_digest = capture(client, method, path, params, request_ids)
            replay, replay_common = capture(client, method, path, params, request_ids)
            if replay != row or common_digest != replay_common:
                raise ValueError(f"{method} {path} 在同一只读事务内重放的结果不同")
            row["repeatDigestVerified"] = True
            public_path = ALIASES.get(path, path)
            key = (method, public_path, digest(params))
            if path in ALIASES:
                if common_digest != digests_by_request[key]:
                    raise ValueError(f"相同参数的 {path} 与 {public_path} 结果不同")
                row.update(pairedPath=public_path, commonResponseSha256=common_digest)
            else:
                digests_by_request[key] = common_digest
            output.append(row)
            if path == "/api/v1/portfolio":
                etag = row["etag"]
                check = client.get(path, headers={"If-None-Match": etag})
                assert check.status_code == 304 and not check.content
                output.append({"method": "GET", "path": path, "ifNoneMatch": etag, "status": 304, "bytes": 0})
    return output


def catalog_examples(factory, catalog):
    grouped = {}
    for item in catalog:
        for access in item["access"]:
            path = access.get("path") or ""
            if access["kind"] != "independent" or not path.startswith("/api/v1/datasets/"):
                continue
            if access["method"] != "GET":
                raise ValueError(f"公共数据集示例应使用 GET: {item['id']}")
            params = access["example"]
            key = (path, digest(params))
            entry = grouped.setdefault(key, {"path": path, "parameters": params, "entries": []})
            entry["entries"].append(item["id"])
    with patch.object(main, "create_engine_and_session", return_value=(factory.kw["bind"].engine, factory)):
        app = main.create_app(background_news=False)
    results = []
    with TestClient(app) as client:
        for key in sorted(grouped):
            entry = grouped[key]
            response = client.get(entry["path"], params=entry["parameters"])
            response.raise_for_status()
            page = response.json()
            if (
                not isinstance(page["data"], list)
                or page["meta"]["dataset"] != entry["path"].rsplit("/", 1)[1]
            ):
                raise ValueError(f"目录示例响应类型或数据集标识不符 {entry}")
            results.append(
                dict(
                    entry,
                    http=response.status_code,
                    rows=len(page["data"]),
                    coverageScope=page["meta"]["coverage_scope"],
                    bodySha256=sha256(response.content).hexdigest(),
                )
            )
    return results
