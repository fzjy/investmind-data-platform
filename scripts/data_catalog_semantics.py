"""数据目录的语义核对：来源、输入表与发布口径须一起维护。仅供离线导出使用。"""
from copy import deepcopy


def audit_semantics(entries, root, table, judge, field):
    by_id = {e["id"]: e for e in entries}

    def add_tables(e, refs):
        seen = {(t["schema"], t["table"], t["fieldPath"]) for t in e["tables"]}
        for ref in refs:
            key = ref["schema"], ref["table"], ref["fieldPath"]
            if key not in seen:
                e["tables"].append(deepcopy(ref))
                seen.add(key)

    def evidence(e, *paths):
        e["evidence"] = list(dict.fromkeys([*e["evidence"], *paths]))

    def inputs(e, ids, *paths):
        """引用已核对的来源条目，保留渠道名/API，补齐输入表而非只列输出快照。"""
        sources = {}
        for id in ids:
            source = by_id[id]
            for s in source["sources"]:
                key = s["name"], s.get("api", ""), s["kind"]
                if key not in sources:
                    sources[key] = deepcopy(s)
                    sources[key]["description"] = "输入条目：" + source["name"] + "。" + s["description"]
            add_tables(e, [t for t in source["tables"] if t["schema"] in {"raw", "core"}])
        e["sources"] = list(sources.values())
        for f in e["fields"]:
            f["sourceRefs"] = list(ids)
        evidence(e, *paths)

    def frequency(e, task, description):
        e["frequencies"] = [dict(name="更新规则", description=description, **({"task": task} if task else {}))]

    def calculation(e, id, name, formula, args, window, missing, path, version="当前构建口径"):
        e["derived"] = judge(True, "包含底座计算的结果，原始输入、计算窗口和缺值条件见下方。")
        e["calculations"].append(dict(id=id, name=name, formula=formula,
            inputs=[dict(id=k, name=v) for k, v in args], window=window,
            version=version, missing=missing, evidence=[path]))
        evidence(e, path)

    def output(e, key, derived, refs, description, unit="不适用（对象或记录集合）"):
        f = next(f for f in e["fields"] if f["id"] == "data." + key)
        f.update(tableRefs=deepcopy(refs), description=description, unit=unit,
                 type="对象或数组，子字段依响应合同", nullable="可包含空值和覆盖状态，空值不表示零")
        f["derived"] = judge(derived, "含底座计算结果，见计算口径" if derived else "按已发布输入选取、组织或映射，未增加指标计算")

    # 同名上游字段必须按所属接口解释，不能复用另一主题的中文含义。
    contextual = {
        "stock-unlock": {"float_share": "解禁股数", "float_ratio": "解禁比例"},
        "stock-repurchase": {"exp_date": "回购失效日期", "vol": "回购数量", "amount": "回购金额"},
        "index-basic": {"exp_date": "指数终止日期", "market": "市场代码", "list_date": "指数发布日期", "weight_rule": "加权方式"},
        "index-weights": {"weight": "成分权重"},
        "commodity-contracts": {"per_unit": "每手交易单位", "multiplier": "合约乘数（国债或指数期货）", "delist_date": "最后交易日期"},
        "etf-shares": {"fd_share": "基金份额（万份）"},
    }
    for id, labels in contextual.items():
        e = by_id[id]
        for f in e["fields"]:
            if f["id"] in labels:
                f["name"] = labels[f["id"]]
                f["description"] = f["name"] + "；上游 " + e["sources"][0]["api"] + "." + f["id"]
    # 采集字段不是全部原样暴露的公共查询字段。
    by_id["stock-unlock"]["access"] = [dict(kind="table", description="当前工作台不单独展示解禁日历。证券、公告日、实际解禁日和解禁股数继续采集，并用于公司行为可见性及发布来源签名；停止新请求的是旧 unlock_date（解禁日期）字段。可核对原件与核心表。")]
    # 修正原先将所有采集主题都指向工作台或市场浏览的占位路径。
    paths = {
        "stock-adjustment": ("/api/v1/stocks/{symbol}", ["data.dailyChart.candles[]（因子用于复权四价，未原样返回因子表）"]),
        "stock-business": ("/api/v1/stocks/{symbol}", ["data.financial.dashboard.businessHistory（主营专题投影及覆盖）"]),
        "stock-forecast": ("/api/v1/stocks/{symbol}", ["data.dailyChart.forecastEvents[]（结构化预告事件，按披露日期定位）"]),
        "stock-repurchase": ("/api/v1/stocks/{symbol}", ["data.financial.shareholders.repurchaseEvidence（回购进展证据）"]),
        "stock-holder-count": ("/api/v1/stocks/{symbol}", ["data.financial.shareholders.concentrationPeriods[]（股东户数与集中度输入）"]),
        "stock-top-holders": ("/api/v1/stocks/{symbol}", ["data.financial.shareholders.periods[]（已披露股东与机构持股投影）"]),
        "stock-dividend": ("/api/v1/stocks/{symbol}", ["data.financial.shareholders.annualDistributions[]（归属财年分红及覆盖）"]),
        "market-margin": ("/api/v1/macro/insights", ["data.extensionInsights.marketSummary（融资余额与流通市值专题）"]),
        "market-daily-summary": ("/api/v1/macro/insights", ["data.extensionInsights.marketSummary（交易所统计专题）"]),
        "market-sz-summary": ("/api/v1/macro/insights", ["data.extensionInsights.marketSummary（深市补充及校准证据）"]),
        "fx-daily": ("/api/v1/macro/insights", ["data.extensionInsights.fx（离岸汇率专题，非原始全量外汇表）"]),
        "stock-classification": ("/api/v1/market/explorer", ["data.heatmapData（行业分组，非原始分类目录）"]),
        "stock-classification-members": ("/api/v1/market/explorer", ["data.heatmapData（按有效行业成员生成的热力图）"]),
    }
    for id, (path, response_paths) in paths.items():
        by_id[id]["access"] = [dict(kind="aggregate", method="GET", path=path, responsePaths=response_paths,
            description="下列为已核对的消费位置；采集原字段不保证逐列对外返回，完整原件请查看对应数据表。")]
    by_id["stock-express"]["access"] = [dict(kind="table", description="默认常规停采，已有历史与显式重放保留；未核准为当前工作台独立展示字段，查看原件及核心表。")]
    for e in entries:
        if e["sources"] and e["sources"][0].get("name") == "Tushare":
            api = e["sources"][0].get("api", "")
            for f in e["fields"]:
                if f["role"] == "source":
                    f["sourceRefs"] = [api + "." + f["id"]]
            if e["frequencies"][0].get("task"):
                task = e["frequencies"][0]["task"]
                frequency(e, task, "按采集任务的生效日程检查来源；行情按交易日，财报及公司行为按披露更新。实际覆盖与最近执行见运行附录。")

    legacy = {
        "realMoneyIndex": ["stock-cashflow", "stock-income"], "freeCashFlow": ["stock-cashflow"],
        "roic": ["stock-income", "stock-balance", "stock-financial-indicator"],
        "pePercentile": ["stock-daily-basic"], "pbPercentile": ["stock-daily-basic"],
        "dividendYears": ["stock-dividend", "stock-basic"], "payoutRatio": ["stock-dividend", "stock-income"],
        "capitalReturnRatio": ["stock-dividend", "stock-repurchase", "stock-cashflow"],
        "expectedDividendYield": ["stock-dividend", "stock-daily"],
        "institutionHolding": ["stock-top-holders", "stock-daily-basic"], "netDebt": ["stock-balance"],
        "profitGrowth": ["stock-income"], "revenueGrowth": ["stock-income"], "cashflowGrowth": ["stock-cashflow"],
    }
    for key, ids in legacy.items():
        e = by_id["metric-" + key]
        inputs(e, ids, "backend/app/stocks/derived/inputs.py")
        frequency(e, None, "历史登记公式随显式派生流水线计算；当前页面是否采用由消费字段决定，没有单独定时任务。")
        e["scope"] = "已经落库的证券、估值日和公式版本；登记公式不代表现行页面采用该算法。"
        for f in e["fields"]:
            f["role"] = "storage"
            f["tableRefs"] = [table("derived", "metric_value", "历史指标结果", "value")]
    by_id["metric-roic"]["calculations"][0]["formula"] = "税后经营利润 ÷ 投入资本；提供上期投入资本时，分母为期初期末均值，否则为本期投入资本"
    by_id["metric-freeCashFlow"]["calculations"][0]["formula"] = "近十二个月经营现金流（cfo_ttm）− 近十二个月资本开支（capex_ttm）"

    card_inputs = {
        "workbenchPeTtm": (["stock-daily", "stock-income", "stock-financial-indicator"], [("close", "估值日收盘价"), ("basic_eps", "连续四季基本每股收益"), ("q_eps", "单季每股收益备选字段")]),
        "workbenchPb": (["stock-daily", "stock-financial-indicator"], [("close", "估值日收盘价"), ("bps", "最近一期每股净资产")]),
        "workbenchMarketCap": (["stock-daily", "stock-daily-basic"], [("close", "估值日收盘价"), ("total_share", "同日总股本"), ("total_mv", "缺股本时反推所用的同日总市值")]),
        "workbenchNetCash": (["stock-balance", "stock-financial-indicator"], [("money_cap", "货币资金"), ("debt_components", "短期借款、长期借款、短期应付债券、应付债券、租赁负债及一年内到期非流动负债"), ("interestdebt", "有息负债总额核对值")]),
        "workbenchFiveYearDividendCount": (["stock-dividend", "stock-basic"], [("cash_div_tax", "正税前每股分红"), ("pay_date", "实际支付日期"), ("end_date", "归属财年"), ("event_identity", "经去重和覆盖核验的分红事件身份")]),
    }
    for key, (ids, args) in card_inputs.items():
        e = by_id["card-" + key]
        inputs(e, ids, "backend/app/stocks/derived/inputs.py")
        e["calculations"][0]["inputs"] = [dict(id=k, name=v) for k, v in args]
        e["calculations"][0]["window"] = {
            "workbenchPeTtm": "估值日可见的最近连续四个季度",
            "workbenchPb": "估值日可见的最近一期财报",
            "workbenchMarketCap": "估值日最近可见行情及同日股本，万股换算为股",
            "workbenchNetCash": "同一报告期的现金及完整有息负债组成",
            "workbenchFiveYearDividendCount": "最近五个已结束财年，按实付正现金事件计数，全年覆盖须核全",
        }[key]
    e = by_id["stock-dividend-yield"]
    inputs(e, ["stock-daily", "stock-daily-basic", "stock-dividend", "stock-income", "stock-financial-indicator"], "backend/app/stocks/derived/inputs.py")

    risk_inputs = {
        "high_cash_high_debt": ["stock-balance", "stock-financial-indicator"], "goodwill_risk": ["stock-balance"],
        "profit_structure_non_operating_dependency": ["stock-income"], "impairment_pressure": ["stock-income"],
        "comprehensive_income_gap": ["stock-income"], "eps_quality_gap": ["stock-income", "stock-financial-indicator"],
        "equity_cash_support": ["stock-cashflow", "stock-balance"], "valuation_band": ["stock-daily-basic", "stock-daily"],
        "interest_debt_burden": ["stock-income", "stock-balance", "stock-financial-indicator"],
        "dividend_vs_financing": ["stock-cashflow", "stock-dividend"],
        "dividend_sustainability": ["stock-cashflow", "stock-dividend"],
        "earnings_quality_non_recurring": ["stock-income", "stock-cashflow", "stock-financial-indicator"],
    }
    for key, ids in risk_inputs.items():
        e = by_id["risk-" + key]
        inputs(e, ids, "backend/app/stocks/derived/risk_inputs.py")
        e["calculations"][0]["inputs"] = [dict(id=id, name=by_id[id]["name"] + "（所需科目见公式及风险卡明细）") for id in ids]
    e = by_id["risk-eps_quality_gap"]
    e["description"] += " 当前扣非 EPS 的供给字段尚未核准，缺失时保持待评估，不能以摊薄 EPS（dt_eps）代替。"
    e["calculations"][0]["missing"] = "缺已核准扣非 EPS（verified_adjusted_eps）或基本 EPS 时为 unknown（待评估），分母为零时留空。"
    e = by_id["risk-earnings_quality_non_recurring"]
    e["description"] += " 当前扣非净利润（verified_adjusted_profit）的供给尚未核准，缺项保持待评估。"
    e["calculations"][0]["missing"] = "缺已核准扣非净利润（verified_adjusted_profit）时为 unknown（待评估），不能把已登记规则当作已具备完整供给。"
    inputs(by_id["stock-ratio-inputs"], ["stock-income", "stock-balance", "stock-cashflow"], "config/stocks/stock-ratio-catalog.json")
    frequency(by_id["stock-ratio-inputs"], "stocks", "随股票财报发布更新；用户假设与试算参数由消费项目管理。")

    fiscal = {"realMoneyIndex": ["stock-cashflow", "stock-income"], "ocfToRevenue": ["stock-cashflow", "stock-income"],
        "ocfToTotalLiab": ["stock-cashflow", "stock-balance"], "capexCoverageRatio": ["stock-cashflow"],
        "cashCollectionRatio": ["stock-cashflow", "stock-income"], "cashPaymentRatio": ["stock-cashflow", "stock-income"],
        "freeCashFlowFy": ["stock-cashflow"], "freeCashFlowMarginFy": ["stock-cashflow", "stock-income"]}
    current = {**fiscal, "pe": ["stock-daily-basic"], "pb": ["stock-daily-basic"],
        "roe": ["stock-financial-indicator"], "roic": ["stock-financial-indicator"],
        "pePercentile": ["stock-daily-basic", "trade-calendar"], "pbPercentile": ["stock-daily-basic", "trade-calendar"],
        "dividendYears": ["stock-dividend", "stock-basic"], "payoutRatio": ["stock-dividend", "stock-income", "stock-daily-basic"],
        "capitalReturnRatio": ["stock-dividend", "stock-repurchase", "stock-cashflow"],
        "institutionHolding": ["stock-top-holders"], "institutionHoldingChange": ["stock-top-holders"], "marketCap": ["stock-daily-basic"]}
    for key, ids in current.items():
        e = by_id["screener-" + key]
        inputs(e, ids)
        if e["derived"]["value"] is True:
            for f in e["fields"]:
                f["derived"] = deepcopy(e["derived"])
        for f in e["fields"]:
            f["tableRefs"] = [table("mart", "stock_screener_daily", "选股发布结果", "fields")]
    for key in ("longMoneyHoldingRatio", "fundHeavyCount", "chipConcentrationChange"):
        e = by_id["screener-" + key]
        e["sources"] = [dict(name="暂无已核准供给", kind="供给缺口", description="仅登记了筛选字段；分类、去重或筹码变化的完整输入与投影尚未建立，当前值为空。")]
        e["derived"] = judge(None, "暂无有效计算结果，输入及公式合同尚未建立，不能判定为来源直接提供。")
        add_tables(e, [table("raw", "source_payload", "可查既有采集原件；尚无该字段完整输入合同")])
        for f in e["fields"]:
            f["derived"] = deepcopy(e["derived"])
            f["description"] = e["sources"][0]["description"]
        frequency(e, None, "暂无可用供给或独立执行日程。")

    e = by_id["security-region"]
    inputs(e, ["stock-basic"], "backend/app/market/marts/region_heatmap.py")
    e["scope"] = "当前证券基础资料的供应商地域标签，可能为城市，未经统一省级映射，也不代表历史注册地。"
    e["history"]["revisions"] = judge(False, "核心证券资料更新当前值；不保存逐次地域变更账本。")
    e = by_id["security-search"]
    inputs(e, ["stock-basic"], "backend/app/search/service.py", "backend/app/search/matching.py")
    e["description"] = "在已发布选股证券池中按代码、名称等匹配候选；请求不发起供应商采集。空关键词返回 idle（等待查询）。"
    e["scope"] = "当前已发布选股池，覆盖范围随发布版本变化。"
    e["history"] = dict(records=judge(False, "不单独存储搜索请求、结果或搜索历史；底层选股发布快照另行保存。"),
        revisions=judge(False, "搜索响应即时生成，不建立修订账本。"), query=judge(False, "搜索仅使用当前发布池，无历史版本参数。"))
    frequency(e, "stocks", "证券候选随选股快照更新，搜索匹配在查询时执行。")
    for f in e["fields"]:
        f["derived"] = judge(False, "搜索词归一化、匹配与分页；未计算金融指标。")
        f["tableRefs"] = [table("mart", "published_snapshot", "已发布选股池", "payload")]

    e = by_id["stock-research"]
    inputs(e, ["stock-daily", "stock-adjustment", "stock-daily-basic", "stock-income", "stock-balance", "stock-cashflow", "stock-financial-indicator", "stock-dividend", "stock-repurchase", "stock-top-holders", "stock-classification", "stock-classification-members"], "backend/app/stocks/research_tools.py", "config/stocks/financial-indicators.json")
    e["scope"] = "同来源、同发布版本、同业务日期的选股池和工作台日线；样本及同行范围由当前发布和指标配置限定。"
    e["history"]["revisions"] = judge(False, "配对研究在请求时计算，不存逐次响应。")
    frequency(e, "stocks", "输入随股票发布更新，配对和统计在请求时计算。")
    calculation(e, "pair-and-rank", "配对价格比与样本统计", "同日价格比 = 当前证券已发布收盘价 ÷ 对比证券已发布收盘价；样本分位 = 不高于当前值的样本数 ÷ 有效样本数 × 100；Z 分数 =（当前值 − 均值）÷ 样本标准差（分母 n−1）",
        [("dailyChart.candles.close", "两只证券已发布日线收盘价"), ("researchValues", "同版选股池已核准研究指标")],
        "价格使用双方真实共同日期；指标按配置的全池或同行有效样本统计", "缺值、样本不足、零方差或版本不一致时留空并说明；价格比未核验完整复权，不代表全收益或溢价。", "backend/app/stocks/research_tools.py")
    for key in ("metrics", "pair"):
        output(e, key, True, [table("mart", "published_snapshot", "已发布选股及工作台输入", "payload")], "同版输入按样本分位、Z 分数或同日价格比计算。")

    market_ids = ["stock-daily", "stock-daily-basic", "stock-basic", "index-daily", "index-global", "etf-daily"]
    e = by_id["market-overview"]
    inputs(e, market_ids, "backend/app/market/marts/market.py", "backend/app/market/sentiment.py", "config/market/market-data.json")
    e["scope"] = "数据库当前覆盖证券池在选定交易日的有效行情。优先最近共同交易日，缺少共同日时取最近可见日；覆盖不足不表示全市场统计。全球指数、ETF 使用各自已配置行情来源。"
    e["description"] = "基于 Tushare 日行情、每日指标和证券资料汇总成交额、涨跌分布；情绪分为底座自算 threshold-v1。涨跌停家数使用来源 limit_status，与阈值情绪计数分别定义；资金流未供给时显示缺失。"
    calculation(e, "market-aggregation", "成交额、宽度与涨跌停统计", "成交额 = 同日非空 amount（成交额，千元）之和；上涨/下跌家数按 pct_chg（涨跌幅）正负计数；平均涨幅 = 非空 pct_chg 算术平均；涨停/跌停家数按 daily_basic.limit_status 的 2/3 与 5/6 计数",
        [("amount", "日成交额（千元）"), ("pct_chg", "日涨跌幅（%）"), ("limit_status", "供应商涨跌停状态码")], "选定交易日和实际覆盖池", "成交额全缺时为空；涨跌停无有效状态时为空，部分覆盖显式列出。", "backend/app/market/marts/market.py")
    calculation(e, "threshold-v1", "阈值情绪分", "R = 上涨数 ÷ 有效数；T = 阈值上涨数 ÷ 有效数；B = 触及阈值后回落数 ÷ 触及阈值数；情绪分 = 100 × [rise_weight × R + threshold_weight × min(T / rise_normalization, 1) + retreat_weight × (1 − min(B / retreat_normalization, 1))]。阈值上涨为 close ≥ pre_close ×（1＋threshold），触及为 high 达到同一阈值，回落为触及但收盘未达到。参数来自 market-data 配置，默认阈值 9.8%、权重 0.60/0.30/0.10、归一化 5%/50%，最终四舍五入为整数。",
        [("close", "收盘价"), ("pre_close", "前收盘价"), ("high", "日最高价"), ("vol", "成交量"), ("list_date", "上市日期"), ("sentiment", "阈值、权重、归一化及等级配置")], "选定交易日；排除停牌（成交量为零）及已知上市首日", "覆盖缺项、重复或无效行情、无有效样本、无人触及阈值时不输出总分，保留计数和原因；阈值不等同各板块法定涨跌停幅度。", "backend/app/market/sentiment.py", "threshold-v1")
    for key in ("turnoverSnapshot", "breadthSnapshots", "sentimentSnapshot"):
        output(e, key, True, [table("core", "daily_price", "当日行情"), table("core", "security", "证券及上市资料")], "数据库已覆盖证券的同日汇总；情绪分按 threshold-v1 计算，见下方公式。")
    for key in ("globalIndices", "etfQuotes"):
        output(e, key, False, [table("core", "market_daily_quote", "配置指数或 ETF 行情")], "按配置品种选取已发布行情，缺失时保留覆盖提示。")
    for key, name in (("turnoverTop10", "成交额前十"), ("limitStats", "涨跌停统计"), ("fundFlowSnapshot", "资金流覆盖状态")):
        f = field("data." + key, name, role="response", derived=key=="limitStats", refs=[] if key=="fundFlowSnapshot" else [table("core", "daily_basic" if key=="limitStats" else "daily_price", "统计输入")], description="资金流当前缺失，不以零代替。" if key=="fundFlowSnapshot" else "按当日实际覆盖行情生成，见统计公式。")
        if key=="fundFlowSnapshot":
            f["derived"] = judge(None, "暂无有效供给；当前是缺失状态占位，不存在可用计算值。")
        e["fields"].append(f)
    frequency(e, "marketPulse", "市场脉冲任务按生效日程重建；股票及市场来源更新提供输入。这里是日线收盘数据，任务执行频率不代表实时行情。")

    e = by_id["market-explorer"]
    inputs(e, [*market_ids, "stock-classification", "stock-classification-members", "commodity-daily", "commodity-mapping", "etf-shares", "news"], "backend/app/market/marts/market.py", "backend/app/market/marts/region_heatmap.py", "backend/app/market/etf/repository.py")
    e["scope"] = "实际覆盖证券按所配行业体系与当前地域标签分组；ETF、商品按各自配置池，未供给部分保留缺失状态。地域可能为城市，不保证统一省级口径或历史时点地域。"
    calculation(e, "heatmap", "热力图与规模换算", "分组涨幅 = 有效成员涨跌幅的算术平均；分组市值/成交额 = 全部成员对应值之和（任一缺项则该合计为空）；股票总市值由万元 ÷ 10000 转亿元，成交额由千元 ÷ 100000 转亿元。ETF 规模 = 基金份额（万份）× 收盘价 ÷ 10000，单位亿元。",
        [("pct_chg", "证券日涨跌幅"), ("total_mv", "证券总市值（万元）"), ("amount", "日成交额（千元）"), ("classification_member", "行业成员及生效区间"), ("security.data.area", "当前地域标签"), ("fd_share", "ETF 份额（万份）")], "同日有效成员；行业成员按有效期过滤，地域使用当前资料", "缺少分类/地域时按覆盖信息处理，地域可进入未知组；不把缺失市值、成交额或份额当零。", "backend/app/market/marts/market.py")
    output(e, "heatmapData", True, [table("core", "daily_price", "行情"), table("core", "daily_basic", "估值"), table("core", "classification_member", "行业成员"), table("core", "security", "地域资料")], "按行业、地域或 ETF 形成分组统计与明细。")
    output(e, "marketNews", False, [table("core", "content_document", "公开快讯")], "默认读取快照快讯；news=latest 时附加读取已入库快讯，不向上游采集。")
    frequency(e, "marketPulse", "随市场重建发布；地域、ETF、商品和快讯分别按对应任务更新输入，具体日程见各来源条目和运行附录。")
    for id in ("market-overview", "market-explorer"):
        by_id[id]["history"] = dict(records=judge(True, "保留已发布页面快照；实际日期和版本依留存记录。"),
            revisions=judge(True, "保存不同发布版本；不记录同一版本内部的每次中间重算。"),
            query=judge(False, "现有市场接口读取当前发布，没有历史日期或 version 参数。"))

    e = by_id["portfolio-inputs"]
    e["name"] = "资产价格、分红与基准公共输入"
    e["description"] = "供消费项目进行组合研究的公共输入包，包含逐证券价格、分红、股息率模型输入、归因输入、全收益基准和国债参考。个人持仓、权重、交易及试算参数和结果由消费项目管理，底座不接受或保存这些私有数据。"
    e["scope"] = "已发布证券池及其可见历史，另含 CNY 业务日面值 1、已配置全收益基准和十年国债参考；输入包按证券组织，不按用户或持仓组织。"
    inputs(e, ["stock-basic", "stock-daily", "stock-adjustment", "stock-daily-basic", "stock-dividend", "stock-income", "stock-financial-indicator", "index-basic", "index-daily", "trade-calendar", "cn.rate.govbond_10y"], "backend/app/portfolio/marts/portfolio.py", "backend/app/portfolio/attribution_inputs.py", "backend/app/portfolio/treasury.py")
    # 公共国债参考装载器固定 chinamoney，不继承基础宏观系列的其他候选渠道。
    e["sources"] = [s for s in e["sources"] if not s["description"].startswith("输入条目：" + by_id["cn.rate.govbond_10y"]["name"] + "。")]
    e["sources"].append(dict(name="中国货币网", api="cn.rate.govbond_10y · chinamoney", kind="国债参考观测", description="仅采用该来源已公开的十年国债收益率，按可见时点选版。"))
    calculation(e, "public-input-transforms", "公共价格及归因输入转换", "复权收盘价 = 未复权收盘价 × 当日复权因子 ÷ 窗口末日因子；归因归母净利润 TTM 按可见报表转单季后取连续四季合计；总股本（万股）× 10000 转为股；十年国债利率（%）÷ 100 转年度小数利率。全收益基准使用供应商全收益指数点位，不由底座混入用户持仓计算。",
        [("daily_price.close", "未复权收盘价"), ("adjustment_factor.adj_factor", "业务日可用及窗口末日复权因子"), ("income_statement.n_income_attr_p", "已披露归母净利润"), ("daily_basic.total_share", "同日总股本（万股）"), ("macro_observation.value", "中国货币网十年国债收益率（%）")], "输入包实际留存窗口，报表及利率按业务日可见时点取版", "原价格转换可沿用之前的有效因子，无可用因子时系数回退为 1；factorCoverage 独立核对每日因子完整性，回退价格不能视为已核验复权。行情、基准和分红缺口保留原因，不能据空事件列表判断零分红。", "backend/app/portfolio/marts/portfolio.py")
    e["calculations"][0]["formula"] = e["calculations"][0]["formula"].replace("当日复权因子", "业务日当天或之前最近可用的有效复权因子").replace("窗口末日因子", "已装载窗口最后有效因子")
    output(e, "prices", True, [table("core", "daily_price", "未复权收盘价", "close"), table("core", "adjustment_factor", "复权因子", "adj_factor")], "按业务日可用因子及窗口最后因子生成价格；可能回退，须结合 factorCoverage 判断实际复权完整性。CNY 为业务日面值 1。")
    output(e, "attributionInputs", True, [table("core", "income_statement", "归母净利润"), table("core", "daily_basic", "同日股本")], "逐证券 TTM 利润与股本等公共输入，附来源身份。用户组合收益归因由消费项目计算。")
    output(e, "treasuryReference", True, [table("core", "macro_observation", "十年国债观测")], "cn.rate.govbond_10y，按公开时点选版并将百分数转换为小数年利率。")
    public_refs = {"assets": ["security"], "unadjustedPrices": ["daily_price"], "factorCoverage": ["adjustment_factor", "daily_price"], "dividendLedger": ["dividend"], "benchmarks": []}
    for key, names in public_refs.items():
        refs = [table("core", n, "公共输入") for n in names] or [table("raw", "source_payload", "全收益指数日线及日历凭证")]
        output(e, key, False, refs, "逐证券公共输入及覆盖凭证；不包含个人持仓或组合结果。" if key!="benchmarks" else "配置的 H00300.CSI、H00922.CSI 全收益指数真实日线及覆盖凭证。")
    output(e, "dividendYieldInputs", True, [table("core", "dividend", "财年分红及查询覆盖"), table("derived", "metric_value", "历史同日 NTM 估算")], "复用逐股分红查询凭证及工作台同日 NTM 估算。模型率以小数提供；历史记录须当时已经留存，不能使用事后重算倒灌的结果。")
    ntm = deepcopy(next(c for c in by_id["stock-dividend-yield"]["calculations"] if c["id"]=="workbenchNtmDividendYield"))
    ntm["formula"] += "；公共模型 rate（估算率）= 已核准 NTM 股息率（%）÷ 100。"
    e["calculations"].append(ntm)
    evidence(e, "backend/app/portfolio/dividend_yield_inputs.py", "backend/app/portfolio/marts/factor_coverage.py")
    e["history"] = dict(records=judge(True, "已采集历期输入及已发布公共快照版本留存，窗口依真实记录。"),
        revisions=judge(True, "保存不同发布版本；同业务键的每次原始修订或中间计算并不保证逐次留存。"),
        query=judge(False, "公共 GET 仅返回当前输入包，载荷含历史点；未提供任意日期或发布版本查询参数。"))
    frequency(e, "stocks", "公共包随股票流水线发布，基准和国债分别按各自来源更新；查询不触发采集或重建。")

    for kind in ("macro", "content"):
        e = by_id["external-" + kind]
        e["sources"] = [dict(name="导入文件声明的 source（来源标识）", kind="外部文件导入", description="由操作者提供原文件及来源、原文链接和发布时间；无固定供应商，导入成功不代表已核验原发布方真实性。")]
        frequency(e, None, "显式 CLI 文件导入，无独立自动采集日程。")
        e["history"]["records"] = judge(True, "保存成功导入的记录及原文件身份，实际日期范围由导入内容决定。")
        e["history"]["revisions"] = judge(None, "宏观 revision（来源修订）与内容 ingestionRevision（导入修订）身份分别处理；不保证已取得发布方每次修改。")

    # 剪刀差继承两条基础系列的配置来源，不能省略真实输入。
    from app.macro.derivations import SPREADS
    for id, _label, left, right, *_rest in SPREADS:
        inputs(by_id[id], [left, right])
        frequency(by_id[id], "macro", "随基础宏观输入发布时计算，观测频率按两侧共同日期。")
    inputs(by_id["macro-spreads"], list(dict.fromkeys(s for row in SPREADS for s in row[2:4])), "backend/app/macro/derivations.py")
    by_id["macro-spreads"]["history"] = deepcopy(by_id[SPREADS[0][0]]["history"])
    frequency(by_id["macro-spreads"], "macro", "随宏观发布对齐基础系列生成，逐项公式见对应剪刀差条目。")

    _enterprise(by_id, inputs, calculation, frequency, table, judge, output, evidence)
    _extensions(by_id, inputs, calculation, frequency, table, judge, evidence, add_tables)

    # 避免末尾新增的字段退回误导性默认值；响应对象和原始字段的语义分开标记。
    for e in entries:
        for f in e["fields"]:
            if not f["sourceRefs"]:
                f["sourceRefs"] = [s.get("api") or s["name"] for s in e["sources"]]
            if f["role"] == "response" and f["derived"]["value"] is False and f["derived"]["explanation"].startswith("来源直接提供"):
                f["derived"] = judge(False, "按来源或发布记录选取、组织；不表示供应商原始响应与此容器结构相同。")
        if e["id"].startswith("card-") or e["id"]=="stock-dividend-yield":
            for f in e["fields"]:
                f["tableRefs"] = [table("derived", "metric_value", "指标卡结果", "value")]
        if e["id"].startswith("risk-"):
            for f in e["fields"]:
                f["tableRefs"] = [table("derived", "risk_result", "当前风险卡及计算明细", "inputs")]
        if e["category"]=="macro" and any(t["table"]=="macro_observation" for t in e["tables"]):
            for f in e["fields"]:
                if not f["tableRefs"]:
                    f["tableRefs"] = [table("core", "macro_observation", "按系列标识及可见版本读取观测", "value")]
                if e["derived"]["value"] is True:
                    f["derived"] = judge(True, "含底座来源转换或专题计算；采用的实际渠道决定是否进行该转换，见计算口径。")
        if all(t["schema"]=="raw" for t in e["tables"]):
            e["history"]["records"] = judge(True, "成功采集后按批次保留原始响应及采集时刻；是否已有原件、业务日期和版本覆盖依实际留存记录。")
    by_id["stock-ratio-inputs"]["history"] = dict(records=judge(False, "比率输入是财报的归一化投影，不单独建立输入历史表；所依赖财报的历期数据另行留存。"),
        revisions=judge(False, "不单独保存每次归一化过程；底层财报版本与发布快照分别核对。"),
        query=judge(False, "从当前工作台获取财报和指标输入，未提供比率输入的任意历史查询。"))
    # 原始交易所摘要/外汇供给由专题执行器管理，不归市场日行情任务统一调度。
    for id in ("market-margin", "market-daily-summary", "market-sz-summary", "fx-daily", "source-cn_gdp"):
        frequency(by_id[id], None, "由宏观专题执行器按专项配置或显式命令采集；专题观测频率和采集检查日程分别说明，配置文件为 macro-extensions.json。")
    for f in by_id["stock-unlock"]["fields"]:
        if f["id"]=="unlock_date":
            f["description"] = "旧合同字段，停止新请求；当前实际解禁日期使用 float_date（解禁日期）。历史原件按原合同解释。"


def _enterprise(by_id, inputs, calculation, frequency, table, judge, output, evidence):
    e = by_id["macro-enterprise"]
    e["name"] = "企业样本与官方指数、ETF 成分范围"
    e["description"] = "对数据池企业财报按同版、同组和可比季度聚合，提供营收利润、库存、应收、现金流、分红与亏损等研究输入。官方目标指指数或 ETF 成分分析对象，不是政策目标；使用已验证官方当前成员与数据池交集重述历史。"
    e["scope"] = "配置企业池、行业或已验证官方指数/ETF 成员与池交集；当前池重述历史，不保证严格历史时点成员。覆盖与质量门槛按企业配置及每季结果。"
    inputs(e, ["stock-basic", "stock-income", "stock-balance", "stock-cashflow", "stock-daily-basic", "trade-calendar", "stock-classification", "stock-classification-members"], "backend/app/macro/enterprise_repository.py", "backend/app/macro/enterprise.py", "backend/app/macro/enterprise_official_targets.py", "backend/app/macro/enterprise_loss.py", "backend/app/macro/enterprise_rea.py", "docs/macro-specialties.md")
    e["sources"].append(dict(name="中证指数及相关基金管理人", kind="官方成分证据", description="官方指数成员文件、基金跟踪指数声明及申赎清单等；具体文档身份按各对象冻结证据，Tushare 候选目录不等同已验证官方成员。"))
    e["tables"].extend([table("raw", "source_payload", "官方成分文件与采集凭证"), table("raw", "enterprise_target_observation", "Tushare 候选响应"), table("core", "enterprise_target_candidate", "候选范围，非已确认官方成员")])
    for kind, label in (("receivable", "应收补充"), ("order", "订单与合同负债补充"), ("rea", "REA 财务补充")):
        e["tables"].extend([table("raw", "enterprise_"+kind+"_response", label+"冻结响应"), table("core", "enterprise_"+kind+"_supplement", label+"核对结果")])
    evidence(e, "backend/app/macro/enterprise_contract.py")
    for f in e["fields"]:
        f["name"] += "（指数或 ETF 分析对象）"
        f["description"] = "官方成分分析对象；经成分文件核验后与数据池求交，不能将候选目录当作已确认成员。"
        f["derived"] = judge(False, "对象标识与成员证据；企业统计在对应范围内计算。")
    calculation(e, "enterprise-cards", "可比企业季度统计", "财务流量转为可比单季、资产存量取期末值。基础 cards.scissors（营收利润剪刀差）的 result = 同组单季营收同比 − 归母净利润同比，消费项目可另行反向展示，不能将展示方向当成接口原值。"
        "cards.white-strip（应收剪刀差）= 同组期末应收账款同比 − 营收同比。cards.lying-flat（基础现金流差）= 单季经营现金流金额 − 资本开支金额，单位亿元；rea.series.lying-flat（企业躺平同比差）= 同组经营现金流同比 − 资本开支同比。"
        "cards.dividend（基础分红扩产差）= 单季资本开支金额 − 分红偿息支付金额，单位亿元；rea.series.dividend（分红挤出扩产同比差）= 分红偿息支付现金同比 − 资本开支同比，保留供应商合并支付项目口径。"
        "cards.inventory（库存卡）比较营收同比和期末库存同比，result 为前者减后者；亏损扩散以报告锚点之后、截止日之前的首个完整交易日为共同估值日，按该日有效成员市值加权。同比按同组同期可比合计计算，不平均公司同比。",
        [("income_statement", "营业收入与归母净利润"), ("balance_sheet", "应收账款与存货期末值"), ("cashflow_statement", "经营现金流、资本开支及分红偿息合并支付"), ("daily_basic.total_mv", "共同估值日总市值"), ("trade_calendar", "上交所开闭市日历，用于确定完整估值日"), ("official_members", "官方当前成员与数据池交集")],
        "可比季度与同组成员；基础卡、REA 展示分支分别定义，不互换同名数值", "财报、成员证据、市场价值或覆盖门槛不足时留空并报告原因；缺估值日历为 valuation-calendar-missing，不能把样本结果解释为全部上市公司。", "backend/app/macro/enterprise.py")
    e["history"] = dict(records=judge(True, "已发布企业季度点与范围质量记录随宏观快照保存。"),
        revisions=judge(True, "保存不同发布版本；旧快照不含每季完整逐行输入引用，不能据此恢复每次财报修订。"),
        query=judge(True, "宏观传导接口可按 version 查询已发布版本，成员历史完整性与版本查询能力分别计量。"))
    frequency(e, None, "随企业宏观发布或显式重建更新；官方成分证据有独立预算及执行开关，见 enterprise-targets 配置，不把基础宏观周任务视为全部专题采集承诺。")


def _extensions(by_id, inputs, calculation, frequency, table, judge, evidence, add_tables):
    definitions = {
        "usdcnh": ("Tushare / FXCM", "fx_daily.bid_close", "离岸人民币汇率买入收盘价，非境内人民币中间价", "日"),
        "vix": ("CBOE，经 FRED 分发", "VIXCLS", "VIX 日收盘，保留 CBOE 归属及用途说明", "日"),
        "governmentBonds": ("中国人民银行", "社会融资规模增量统计表", "政府债券融资月增量及原文件", "月"),
        "householdCredit": ("中国人民银行", "金融统计报告", "人民币住户中长期贷款累计额及可核对的当月净增", "月"),
        "tier1Housing": ("国家统计局", "70 个大中城市住宅销售价格报告", "一线城市新房与二手住宅环比，采用官方汇总", "月"),
        "specialBonds": ("财政部", "预算限额、债券发行统计和发布会", "新增专项债年度额度、月发行、累计发行及进度", "月，额度按年"),
        "propertyLoans": ("中国人民银行", "金融机构贷款投向统计报告", "房地产贷款余额和同比，缺正式报告时不供数", "季"),
        "marginBalances": ("Tushare / 交易所统计", "margin.rzye · daily_info.float_mv · sz_daily_info.float_mv", "沪深融资余额与同日 A 股流通市值；融资分子可能含基金，北交所余额单列", "日，面板按月对齐"),
        "aShareFloatMv": ("Tushare / 交易所统计", "daily_info.float_mv · sz_daily_info.float_mv", "沪深 A 股流通市值；深市补充板块须单位校验后使用", "日，面板按月对齐"),
        "nominalGdp": ("国家统计局，经 Tushare 分发", "cn_gdp.gdp · cn_gdp.gdp_yoy", "累计名义 GDP 与实际 GDP 同比；平减指标须同季且版本匹配", "季"),
    }
    for gid, (name, api, description, freq) in definitions.items():
        e = by_id["macro-extension-" + gid]
        e["sources"] = [dict(name=name, api=api, kind="专题来源", description=description)]
        add_tables(e, [table("core", "macro_observation", "专题标准化观测"), table("ops", "job_run", "专题执行及冻结来源证据")])
        e["scope"] = "按该专题配置的回看窗口与成功留存记录；不代表窗口完整或最新一期已经取得。"
        e["frequencies"].insert(0, dict(name="观测频率", description=freq + "频；与下方采集检查日程分别计量"))
        e["history"] = dict(records=judge(True, "保存成功留存原件、标准化观测和已发布专题点；缺期保留为空。"),
            revisions=judge(None, "可保存不同来源及发布版本，但不保证发布方每次修订均已采集或每次中间重算都留存。"),
            query=judge(True, "宏观聚合接口支持已发布 version；载荷历史长度受原件覆盖限制。"))
        evidence(e, "backend/app/macro/extensions/view.py")
    formulas = {
        "usdcnh": ("汇率变化", "（最新买入收盘价 ÷ 上一个实际观测买入收盘价 − 1）× 100", [("bid_close", "离岸人民币买入收盘价")], "相邻实际观测，不保证连续交易日", "backend/app/macro/extensions/view.py"),
        "vix": ("VIX 变化", "（最新 VIX 日收盘值 ÷ 上一个实际观测值 − 1）× 100", [("VIXCLS", "VIX 日收盘值")], "相邻实际观测", "backend/app/macro/extensions/view.py"),
        "householdCredit": ("累计转当月", "同年当月累计净增 − 上月累计净增；1 月可直接采用当年累计", [("household_ytd", "住户中长期贷款同年累计净增")], "同年相邻月，不跨年相减", "backend/app/macro/official_macro_normalize.py"),
        "specialBonds": ("专项债进度与月差", "计算进度 = 累计新增专项债发行 ÷ 同年年度额度 × 100；同年相邻累计差作为推算当月额，与直报当月额冲突时保留差异及缺值；发布会直报进度单列，不替换计算值", [("quota", "年度新增专项债限额"), ("issued_ytd", "年初累计新增专项债发行额"), ("issued_month", "当月发行直报额"), ("reported_progress", "发布会直报进度")], "同年度、相邻月份及可匹配文件版本", "backend/app/macro/official_macro_normalize.py"),
        "nominalGdp": ("单季 GDP 与平减指标", "第一季金额 = 当季累计；其他季金额 = 当季累计 − 上季累计。名义同比 =（单季金额 ÷ 上年同季金额 − 1）× 100；近似平减差 = 名义同比 − 实际同比；严格平减变化率 =［（1＋名义同比/100）÷（1＋实际同比/100）−1］×100。平减结果不是价格指数水平。", [("gdp", "名义 GDP 年内累计额"), ("gdp_yoy", "同季实际 GDP 同比"), ("cn.gdp.real_yoy", "基础实际 GDP 同比及版本核对值")], "真实同季、累计转单季且实际增速版本相符", "backend/app/macro/extensions/view.py"),
    }
    for gid, (name, expression, args, window, path) in formulas.items():
        e = by_id["macro-extension-" + gid]
        calculation(e, gid + "-transform", name, expression, args, window, "缺输入、版本不符、无有效分母或相邻期缺失时留空并保留原因。", path)
        e["fields"][0]["derived"] = deepcopy(e["derived"])
    for gid in ("marginBalances", "aShareFloatMv"):
        e = by_id["macro-extension-" + gid]
        calculation(e, "financing-to-float-mv", "融资余额与流通市值同日比值", "100 ×（沪市融资余额＋深市融资余额，元）÷［100000000 ×（沪市 A 股流通市值＋深市 A 股流通市值，亿元）］；面板按月选取真实共同交易日。融资余额可能含基金，北交所单列，不能称为纯 A 股融资比例。",
            [("margin.rzye", "交易所融资余额（元）"), ("daily_info.float_mv", "沪深 A 股流通市值（亿元）"), ("sz_daily_info.float_mv", "深市板块补充，单位须经官方样例标定")], "真实同日输入的月对齐窗口，不向前填充", "缺同日输入或深市补充未校准时留空。", "backend/app/macro/exchange_summary_view.py")
        e["fields"][0]["derived"] = deepcopy(e["derived"])
    for gid in ("governmentBonds", "tier1Housing", "propertyLoans"):
        by_id["macro-extension-" + gid]["derived"] = judge(False, "采用官方已披露的该指标数值；官方本身的统计方法不计为底座派生。")
