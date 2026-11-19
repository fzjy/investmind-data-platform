"""从生产路由离线导出公共 v1 请求合同及可阅读的参数索引。"""
import json
from pathlib import Path

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
TOPICS = {
    "/api/v1/data-status": ("数据获取状态", "platform-api.md#数据获取状态"),
    "/api/v1/news": ("市场快讯", "news-api.md"),
    "/api/v1/market/overview": ("市场总览", "market-supply.md"),
    "/api/v1/market/explorer": ("市场浏览、商品及热力图", "market-supply.md"),
    "/api/v1/macro/transmission": ("宏观传导", "macro-supply.md"),
    "/api/v1/macro/insights": ("宏观观察、企业范围及采集状态", "macro-specialties.md"),
    "/api/v1/portfolio": ("组合公共输入", "portfolio-split-lineage.md"),
    "/api/v1/stocks/research": ("证券研究与配对", "workbench-api.md"),
    "/api/v1/stocks/screener": ("股票筛选、排序与分页", "screener-api-fields.md"),
    "/api/v1/stocks/{symbol}": ("个股工作台", "workbench-api.md"),
    "/api/v1/datasets/stock-daily": ("股票未复权日行情", "stock-datasets.md"),
    "/api/v1/datasets/stock-adjustment": ("股票复权因子", "stock-datasets.md"),
    "/api/v1/datasets/stock-daily-basic": ("股票每日估值与股本", "stock-datasets.md"),
    "/api/v1/datasets/stock-securities": ("证券主数据与曾用名", "foundation-stock.md"),
    "/api/v1/datasets/stock-trade-calendar": ("交易所日历", "foundation-stock.md"),
    "/api/v1/datasets/stock-classification-members": ("分类成员与生效期", "foundation-stock.md"),
    "/api/v1/datasets/stock-financial-reports": ("财务报表及已存版本", "foundation-stock.md"),
    "/api/v1/datasets/stock-dividends": ("分红事件与时点证据", "foundation-stock.md"),
    "/api/v1/datasets/stock-disclosures": ("财报披露日程", "foundation-stock.md"),
    "/api/v1/datasets/stock-company-events": ("公司公告事件", "foundation-stock.md"),
    "/api/v1/datasets/market-daily": ("指数和 ETF 日行情", "foundation-markets-macro.md"),
    "/api/v1/datasets/etf-trading": ("ETF 量额与基金份额", "foundation-markets-macro.md"),
    "/api/v1/datasets/commodity-daily": ("商品主力合约日线", "foundation-markets-macro.md"),
    "/api/v1/datasets/stock-limit-status": ("股票涨跌停状态", "foundation-markets-macro.md"),
    "/api/v1/datasets/macro-observations": ("标准宏观系列与修订", "foundation-markets-macro.md"),
    "/api/v1/datasets/macro-specialty-observations": ("专项宏观观测与来源时点", "foundation-markets-macro.md"),
    "/api/v1/datasets/content-documents": ("可展示新闻、公告与文章", "foundation-content.md"),
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


def schema_bounds(schema):
    # 可空查询参数的格式、正则和长度约束位于 anyOf 的非空分支。
    variants = [item for item in schema.get("anyOf", []) if item.get("type") != "null"]
    if len(variants) == 1:
        schema = {**variants[0], **schema}
    keys = ("default", "minimum", "maximum", "format", "minLength", "maxLength", "pattern", "minItems", "maxItems")
    return ", ".join(f"{key}={json.dumps(schema[key], ensure_ascii=False)}" for key in keys if key in schema)


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
        if path.rsplit("/", 1)[-1] in {"stock-daily", "stock-adjustment", "stock-daily-basic"}:
            lines.extend(["查询单日时传 `trade_date`，可加 `symbol`。查询单股区间时同时传 `symbol`、`start_date` 和 `end_date`，包含首尾两日。日期使用 `YYYY-MM-DD`。下一页沿用相同条件，将 `meta.next_cursor` 传给 `cursor`。", ""])
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
            bounds = schema_bounds(schema).replace("|", r"\|")
            lines.append(f"| {origin} | `{key}` | {'是' if required else '否'} | {schema_label(schema)} | {bounds or '见请求模型'} |")
        lines.append("")
    (out / "api-requests.md").write_text("\n".join(lines) + "\n", encoding="utf8")


if __name__ == "__main__":
    generate()
