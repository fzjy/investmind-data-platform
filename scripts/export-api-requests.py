"""从生产路由离线导出公共 v1 请求合同及可阅读的参数索引。"""
import json
from pathlib import Path

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
TOPICS = {
    "/api/v1/news": ("市场快讯", "news-api.md"),
    "/api/v1/market/overview": ("市场总览", "market-supply.md"),
    "/api/v1/market/explorer": ("市场浏览、商品及热力图", "market-supply.md"),
    "/api/v1/macro/transmission": ("宏观传导", "macro-supply.md"),
    "/api/v1/macro/insights": ("宏观观察、企业范围及采集状态", "macro-specialties.md"),
    "/api/v1/portfolio": ("组合公共输入", "portfolio-split-lineage.md"),
    "/api/v1/stocks/research": ("证券研究与配对", "workbench-api.md"),
    "/api/v1/stocks/screener": ("股票筛选、排序与分页", "screener-api-fields.md"),
    "/api/v1/stocks/{symbol}": ("个股工作台", "workbench-api.md"),
    "/api/v1/search": ("证券搜索", "security-search.md"),
}


def schema_label(schema):
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "anyOf" in schema:
        return " / ".join(schema_label(choice) for choice in schema["anyOf"])
    if "enum" in schema:
        return " / ".join(map(str, schema["enum"]))
    if schema.get("type") == "array":
        return f"数组（{schema_label(schema['items'])}）"
    return schema.get("type", "对象")


def generate():
    spec = app.openapi()
    spec["paths"] = {path: item for path, item in spec["paths"].items() if path.startswith("/api/v1/")}
    if set(TOPICS) != set(spec["paths"]):
        raise ValueError("请为新增或变动的 v1 路由登记用途与供给文档")
    out = ROOT / "docs"
    (out / "openapi-v1.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    lines = ["# 公共数据 API 请求参数", "", "由生产 FastAPI 路由离线导出。各接口旁的供给文档记录返回字段、单位、原字段、数据库位置、更新节奏和历史边界。[机器可读请求规范](openapi-v1.json) · [字段消费者](field-consumers.md)", ""]
    schemas = spec["components"]["schemas"]
    for path, (name, target) in TOPICS.items():
        method, route = next(iter(spec["paths"][path].items()))
        lines.extend([f"## {name}", "", f"`{method.upper()} {path}` · [返回字段与供给说明]({target})", ""])
        params = [(p["in"], p["name"], p.get("required", False), p["schema"]) for p in route.get("parameters", [])]
        if "requestBody" in route:
            body = route["requestBody"]["content"]["application/json"]["schema"]
            model = schemas[schema_label(body)]
            params.extend(("body", key, key in model.get("required", []), value) for key, value in model["properties"].items())
        if not params:
            lines.extend(["无需请求参数。", ""])
            continue
        lines.extend(["| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |", "| --- | --- | --- | --- | --- |"])
        for origin, key, required, schema in params:
            bounds = ", ".join(f"{k}={json.dumps(schema[k], ensure_ascii=False)}" for k in ("default","minimum","maximum","minLength","maxLength","pattern","minItems","maxItems") if k in schema)
            lines.append(f"| {origin} | `{key}` | {'是' if required else '否'} | {schema_label(schema)} | {bounds or '见请求模型'} |")
        lines.append("")
    (out / "api-requests.md").write_text("\n".join(lines) + "\n", encoding="utf8")


if __name__ == "__main__":
    generate()
