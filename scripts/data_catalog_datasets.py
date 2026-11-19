"""给读者提供可直接使用的分域数据集示例，字段以 FastAPI 响应模型为准。"""
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


def register_domain_queries(entries, spec):
    by_id = {entry["id"]: entry for entry in entries}
    schemas = spec["components"]["schemas"]
    dates = {"start_date": "2026-10-01", "end_date": "2026-10-16"}
    stock = {"symbol": "600519.SH"}
    reports = {**stock, "start_date": "2025-01-01", "end_date": "2026-10-16"}

    def add(topic, dataset, params, meaning):
        entry = by_id[topic]
        path = "/api/v1/datasets/" + dataset
        operation = spec["paths"][path]["get"]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        props = schemas[response["$ref"].split("/")[-1]]["properties"]
        row = schemas[props["data"]["items"]["$ref"].split("/")[-1]]
        meta = schemas[props["meta"]["$ref"].split("/")[-1]]
        paths = [f"data[].{key}" for key in row["properties"]] + [f"meta.{key}" for key in meta["properties"]]
        entry["access"].append(dict(kind="independent", method="GET", path=path, example=params, responsePaths=paths))
        # 一个条目可能同时有事实、产品和多个不同时间口径的读取方式。
        history = entry["history"]["query"]
        if topic == "market-overview" or dataset == "stock-securities":
            history["explanation"] += " " + meaning
        elif history.get("value") is not True:
            history.update(value=True, explanation=meaning)
        elif meaning not in history["explanation"]:
            history["explanation"] += " " + meaning

    add("stock-basic", "stock-securities", stock, "证券主表提供当前属性及已存曾用名。")
    add("trade-calendar", "stock-trade-calendar", {**dates, "exchange": "SSE"}, "可按交易所与自然日区间查询。")
    for topic in ("stock-classification", "stock-classification-members"):
        add(topic, "stock-classification-members", {**stock, **dates}, "可查询与区间相交的已存成员关系。")
    for topic, statement in (("stock-income", "income"), ("stock-balance", "balance"),
                             ("stock-cashflow", "cashflow"), ("stock-financial-indicator", "indicator")):
        add(topic, "stock-financial-reports", {**reports, "statement": statement}, "可按报告期返回库存版本。")
    add("stock-dividend", "stock-dividends", reports, "可按公告日查询库存，as_of 视图另按捕获凭证判定。")
    add("stock-disclosure", "stock-disclosures", reports, "可按证券及报告期查询库存披露日程。")
    for kind in ("forecast", "express", "repurchase", "unlock"):
        sample = {"express":"603019.SH", "unlock":"000333.SZ"}.get(kind, stock["symbol"])
        add("stock-" + kind, "stock-company-events", {**reports, "symbol":sample, "kind":kind},
            "可按证券、类别及公告日查询库存事件。")
    for topic, kind, code in (("index-daily", "indices", "000001.SH"), ("index-global", "indices", "SPX"),
                              ("etf-daily", "etfs", "510300.SH")):
        add(topic, "market-daily", {**dates, "asset_type":kind, "asset_code":code}, "可按单个品种查询日线。")
    for topic in ("etf-daily", "etf-shares"):
        add(topic, "etf-trading", {"asset_code":"510300.SH", "trade_date":"2026-10-16"}, "可查专项日行情量额与份额。")
    for topic in ("commodity-daily", "commodity-mapping"):
        add(topic, "commodity-daily", {**dates, "asset_code":"AU.SHF", "window_end":"2026-10-16"},
            "按已核验窗口查主力合约及其日线。")
    for topic in ("stock-daily-basic", "market-overview"):
        add(topic, "stock-limit-status", {**stock, "trade_date":"2026-10-16"}, "可按股票和交易日查状态来源。")
    for series in MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).series:
        add(series.id, "macro-observations", {"series_id":series.id, "start_date":"2024-01-01",
            "end_date":"2026-10-16", "as_of":"2026-10-21T00:00:00Z"}, "可按观测期和发布时间截止点选版。")
    for topic, series in (("macro-extension-nominalGdp", "cn.gdp.nominal_cumulative"),
                          ("macro-extension-usdcnh", "fx.usdcnh")):
        add(topic, "macro-specialty-observations", {"series_id":series, "start_date":"2024-01-01",
            "end_date":"2026-10-16", "as_of":"2026-11-08T14:51:38Z"}, "可按专项已核来源及截止时刻查询。")
    for topic, params in (("news", {"content_type":"news"}), ("external-content", {"content_type":"article"}),
                          ("stock-announcements", {"content_type":"announcement"})):
        add(topic, "content-documents", {**params, "limit":5}, "可按发布时间筛选当前允许展示的库存。")
