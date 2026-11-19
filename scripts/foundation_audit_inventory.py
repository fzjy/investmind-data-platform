"""把目录逐项关联到真实库存、查询代码、消费位置和注册入口。"""

import ast
from collections import Counter, defaultdict
from pathlib import Path
import re
import runpy
import sys

from fastapi import FastAPI
from sqlalchemy import text
from typer.main import get_command


def routes(app, prefix=""):
    out = []
    for route in app.routes:
        if router := getattr(route, "original_router", None):
            out.extend(routes(router, prefix + route.include_context.prefix))
        elif hasattr(route, "methods"):
            out.append(
                {
                    "path": prefix + route.path,
                    "methods": sorted(route.methods),
                    "handler": route.endpoint.__module__ + "." + route.endpoint.__qualname__,
                }
            )
        elif getattr(route, "routes", None):
            out.extend(routes(route, prefix + route.path))
        elif hasattr(route, "path"):
            out.append({"path": prefix + route.path, "methods": [], "handler": type(route).__name__})
    return out


def object_sources(root, objects):
    files = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf8")
        for folder in ("backend/app", "backend/migrations")
        for p in (root / folder).rglob("*.py")
    }
    names = {}
    for path in files:
        if "/db/models/" not in path:
            continue
        for cls in ast.walk(ast.parse(files[path])):
            if isinstance(cls, ast.ClassDef):
                match = re.search(
                    r"__tablename__\s*=\s*['\"]([^'\"]+)", ast.get_source_segment(files[path], cls) or ""
                )
                if match:
                    names[match[1]] = cls.name
    for obj in objects:
        model = names.get(obj["name"])
        parts = [re.escape(obj["rel"])]
        if model:
            parts.append(re.escape(model))
        regex = re.compile(r"\b(?:" + "|".join(parts) + r")\b")
        obj["model"] = model
        obj["codeReferences"] = [
            path + ":" + str(contents.count("\n", 0, m.start()) + 1)
            for path, contents in files.items()
            if (m := regex.search(contents))
        ]
        obj["migrations"] = [
            path
            for path, contents in files.items()
            if "migrations/versions" in path and re.search(r"\b" + re.escape(obj["name"]) + r"\b", contents)
        ]
        if not obj["migrations"] and obj["schema"] != "public":
            if obj["schema"] in ("derived", "mart"):
                creation = "20260817_0002_derived_and_marts.py"
            elif obj["rel"] in (
                "raw.external_file",
                "raw.external_record",
                "core.content_document",
                "core.macro_observation",
                "derived.macro_xray_result",
            ):
                creation = "20260817_0003_external_import.py"
            else:
                creation = "20260817_0001_data_foundation.py"
            obj["migrations"].append("backend/migrations/versions/" + creation)
        if not obj["migrations"]:
            obj["migrationNote"] = "基表集合通过元数据迁移建表；具体 DDL 及当前约束见 database.json"


RETAINED = {
    "stock-disclosure": "披露日历按报告期更新，旧的预定和修改日期无法完整回溯。",
    "stock-express": "快报稀疏，留有当期库存及字段证据；与正式报告分别解释。",
    "stock-forecast": "预告的上下限、预告类型和原因由业绩披露合同处理；保留专项详情的合并规则。",
    "stock-business": "分部收入需按产品/地区口径拆分，专项补充名称与收入并带原件核验。",
    "stock-repurchase": "回购计划与实际执行由进度和公告区分；继续供应历史资本回报计算。",
    "stock-unlock": "原件与 Core 供历史股本事件核对；公告日、流通日和旧 unlock_date 不同，保留各自来源。",
    "stock-holder-count": "户数按报告期留存；部分原公告时间字符串未写入标准日期，保留原件。",
    "stock-top-holders": "同一股东多期记录与机构分类用于持仓分析；股东类别由专项合同解释。",
}
SPREADS = {
    "cn.ppi_cpi_spread",
    "cn.m2_m1_spread",
    "cn.money.m1_minus_m2_yoy",
    "cn.govbond_dr007_spread",
    "us.treasury_fed_funds_spread",
    "cn.us.rate_spread",
}


def decision(e):
    ident = e["id"]
    tables = [x["schema"] for x in e["tables"]]
    if any((a.get("path") or "").startswith("/api/v1/datasets/") for a in e["access"]) and ident in RETAINED:
        return "公共事实及内部证据", RETAINED[ident] + " 对经合同登记的字段提供库存查询。"
    if ident in ("macro-extension-nominalGdp", "macro-extension-usdcnh"):
        return (
            "公共事实及产品适配",
            "提供已完成任务且与 Core 核对一致的观测。增长率、汇率变化等产品计算保留在专题。",
        )
    if ident in RETAINED:
        return "保留现有领域能力", RETAINED[ident]
    if ident in ("etf-shares", "commodity-mapping", "commodity-daily"):
        return (
            "公共事实及内部证据",
            "开放具备完整来源的已核验交易数据及其来源引用；供给目录及逐次专项证据保持内部。",
        )
    if ident == "external-content":
        return "公共事实及内部证据", "可展示导入文档按内容合同读取。导入源文件、原始内容和逐行校验属于内部。"
    if ident == "external-macro":
        return "内部证据", "文件及逐行导入回执。通过已规范化的宏观观测或可展示文档对外提供事实。"
    if ident == "source-cn_gdp":
        return "内部证据", "同源 GDP 原件已映射到观测及名义 GDP 专题，保留来源版本及字段口径。"
    if ident == "stock-bse-mapping":
        return "保留专项", "北交所新旧代码的证据与组合别名核验绑定；按证券身份消费。"
    if ident == "security-region":
        return (
            "保留现有领域能力",
            "证券地区只在专项标注来源后供市场热力图聚合；公共证券查询目前不承诺地区字段。",
        )
    if ident == "market-overview":
        return "公共事实及产品适配", "涨跌停状态可逐股查询。市场宽度、榜单和市场表现按概览版本发布。"
    if ident in ("stock-announcements", "news"):
        return (
            "公共事实及产品适配",
            {
                "security-region": "自带地区来源的证券资料；地图聚合及未知地区分组由产品负责。",
                "stock-announcements": "公开可展示公告的身份、时间和链接；图表标记、回填和留存清理由产品负责。",
                "news": "共享已获展示许可的库存文档；中文来源、展示时间、采集健康及旧游标由产品负责。",
            }[ident],
        )
    if ident.startswith("metric-"):
        return (
            "保留历史派生结果",
            "保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。",
        )
    if ident in (
        "security-search",
        "market-explorer",
        "market-overview",
        "portfolio-inputs",
        "stock-research",
        "stock-ratio-inputs",
        "stock-dividend-yield",
        "macro-enterprise",
        "macro-spreads",
    ) or ident.startswith(("card-", "risk-", "screener-", "metric-")):
        return (
            "InvestMind 产品或适配",
            "按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。",
        )
    retained_topics = {
        "macro-extension-vix": "专题保留 CBOE 来源及 FRED 分发归属和用途限制；在专题内展示有来源的观测。",
        "macro-extension-governmentBonds": "官方社会融资规模增量表按月份和资料版次留存；缺文件的月份保留缺口。",
        "macro-extension-householdCredit": "贷款统计报告按文档及季末兜底选取；抓取失败与真实零值须区分。",
        "macro-extension-tier1Housing": "70 城报告按城市、基期和价格口径核对；目录与报告有独立凭证。",
        "macro-extension-specialBonds": "预算限额、发行与余额的表头、期间及计量口径分别留证。",
        "macro-extension-propertyLoans": "季度贷款投向报告的范围和同比增速随报告定义；保留官方文档依据。",
        "macro-extension-marginBalances": "两融余额与流通市值跨所对齐，在指定发布滞后后计算比率。",
        "macro-extension-aShareFloatMv": "沪深两市需同日且单位一致；未证实的一侧不可填零。",
        "market-daily-summary": "交易所总貌供宏观流通市值等汇总。统计范围、发布时间独立于逐股行情。",
        "market-sz-summary": "深交所总貌由对应交易所日历与单位校准后供专题使用。",
        "market-margin": "两融余额按交易所及交易日采集，专题使用可比的流通市值分母。",
        "fx-daily": "USD/CNH 的 bid_close 通过宏观专项查询；其他货币对、价侧和日期范围保留原件。",
        "etf-basic": "未留存该 API 样本，配置与基金日行情、份额是不同对象。",
        "index-basic": "基准目录按代码、全收益口径与组合清单核验，保留可追溯的识别凭证。",
        "index-weights": "未留存该 API 的权重原件，目录只证明来源合同。",
        "commodity-calendar": "主力合约窗口要对照完整期货开市日历；保留日历作为逐日商品数据的完整性证据。",
        "commodity-contracts": "商品日行情使用经过校验的合约及报价单位；全合约目录不按行情端点开放。",
    }
    if ident in retained_topics:
        return "保留专项", retained_topics[ident]
    if ident.startswith("macro-extension-"):
        return (
            "保留专项",
            "专题变换与证据门禁按现有专题读取。合格序列走宏观观测合同；原件及拒收状态保留内部。",
        )
    if e["category"] == "macro":
        if ident in SPREADS:
            return (
                "InvestMind 产品或适配",
                "差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。",
            )
        return "公共数据集", "宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。"
    if tables == ["raw"]:
        return (
            "保留专项",
            "现存证据为供应商原件；原件保留在内部，按已登记的专题消费。未证明可独立发布的标准化字段不开放。",
        )
    return "公共数据集", "按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。"


def witness(s, e, relmap, raw):
    ident = e["id"]
    query = None
    params = {}
    for prefix, t, col in (
        ("metric-", "derived.metric_value", "metric_id"),
        ("card-", "derived.metric_value", "metric_id"),
        ("risk-", "derived.risk_result", "risk_id"),
    ):
        if ident.startswith(prefix):
            query = f"select count(*) from {t} where {col}=:key"
            params = {"key": ident[len(prefix) :]}
    if ident.startswith("screener-"):
        query = "select count(*) from mart.stock_screener_daily where fields::jsonb ? :key or (fields->'researchValues')::jsonb ? :key"
        params = {"key": ident.removeprefix("screener-")}
    if e["category"] == "macro" and "." in ident and ident not in SPREADS:
        query = "select count(*) from core.macro_observation where series_id=:key"
        params = {"key": ident}
    special = {
        "stock-announcements": (
            "select count(*) from core.content_document where content_type='announcement'",
            {},
        ),
        "news": ("select count(*) from core.content_document where content_type='news'", {}),
        "security-region": ("select count(*) from core.security where data->>'area' is not null", {}),
        "external-macro": ("select count(*) from raw.external_record where record_type='macro'", {}),
        "external-content": ("select count(*) from raw.external_record where record_type='content'", {}),
        "stock-dividend-yield": (
            "select count(*) from derived.metric_value where metric_id in ('workbenchFiscalDividendYield','workbenchAnnualDividendYield','workbenchNtmDividendYield')",
            {},
        ),
    }
    if ident in SPREADS:
        query = "select count(*) from mart.page_snapshot where product='macro-insights' and strpos(payload::text,:key)>0"
        params = {"key": ident}
    if ident in ("etf-basic", "index-weights"):
        query = "select count(*) from raw.source_payload where api_name=:key"
        params = {"key": e["sources"][0]["api"]}
    if ident in special:
        query, params = special[ident]
    if ident.startswith("macro-extension-"):
        query = "select count(*) from raw.ingestion_batch where split_part(dataset,':',2)=:key"
        params = {"key": ident.removeprefix("macro-extension-")}
    if ident in (
        "market-explorer",
        "market-overview",
        "portfolio-inputs",
        "macro-spreads",
        "macro-enterprise",
    ):
        query = "select count(*) from mart.published_snapshot where product=:key"
        params = {
            "key": {
                "portfolio-inputs": "portfolio",
                "macro-spreads": "macro-insights",
                "macro-enterprise": "macro-transmission",
            }.get(ident, ident)
        }
    if query:
        return {"query": query, "params": params, "rows": s.scalar(text(query), params)}
    primary = e["tables"][0]
    rel = primary["schema"] + "." + primary["table"]
    if rel.startswith("core.") and e["category"] == "stocks":
        return {"relation": rel, "rows": relmap[rel]["rows"], "scope": "主事实表"}
    api_names = {src.get("api") for src in e["sources"]}
    caps = [r for r in raw if r["api_name"] in api_names]
    if caps:
        return {
            "scope": "输入来源的捕获样本，未断言输出非空",
            "rawIds": [r["sample"] for r in caps],
            "captures": sum(r["captures"] for r in caps),
        }
    return {"scope": "本项以目录所列构建器、字段合同及其已固定的产品响应为依据，未单独计数"}


def write_paths(e):
    """入口用于定位写者。历史运行归属必须另查批次 ID 或任务凭证。"""
    ident = e["id"]
    exact = {
        "news": ["sync-news", "watch-news", "import-jsonl"],
        "external-content": ["import-jsonl"],
        "external-macro": ["import-jsonl"],
        "stock-announcements": ["sync-announcements", "import-jsonl"],
        "stock-research": ["rebuild-stock-research"],
        "security-search": ["build-and-publish"],
        "security-region": ["sync-security-region"],
        "stock-business": ["sync-business-segments", "backfill-universe"],
        "stock-classification": ["sync-classification"],
        "stock-classification-members": ["sync-classification"],
        "stock-dividend": [
            "backfill-universe",
            "sync-stock-data",
            "capture-portfolio-dividends",
            "update-routine",
        ],
        "stock-bse-mapping": ["update-routine"],
        "stock-dividend-yield": ["rebuild-workbench-metric-cards", "build-and-publish"],
        "stock-daily-basic": [
            "sync-stock-data",
            "sync-market-pulse",
            "sync-valuation-history",
            "backfill-universe",
        ],
        "macro-enterprise": [
            "sync-enterprise-target-evidence",
            "sync-enterprise-orders",
            "sync-enterprise-receivables",
            "sync-enterprise-rea-inputs",
            "sync-enterprise-official-members",
            "sync-enterprise-fund-members",
            "sync-enterprise-catalog-members",
        ],
        "macro-spreads": ["sync-macro", "rebuild-external-marts"],
        "portfolio-inputs": [
            "capture-portfolio-dividends",
            "capture-portfolio-reference-catalog",
            "capture-portfolio-reference-history",
            "rebuild-portfolio",
            "update-routine",
        ],
        "market-overview": ["rebuild-market-marts", "sync-market-pulse"],
        "market-explorer": [
            "rebuild-market-marts",
            "sync-market-data",
            "sync-commodities",
            "sync-etf-heatmap",
        ],
        "stock-income": ["sync-stock-data", "backfill-universe", "sync-financial-statements"],
        "stock-cashflow": ["sync-stock-data", "backfill-universe", "sync-financial-statements"],
        "stock-balance": ["sync-stock-data", "backfill-universe", "sync-financial-statements"],
        "source-cn_gdp": ["sync-tushare-macro", "sync-macro-extensions"],
        "etf-basic": [],
        "index-weights": [],
        "market-daily-summary": ["sync-macro-extensions"],
        "market-sz-summary": ["sync-macro-extensions"],
        "fx-daily": ["sync-macro-extensions"],
        "market-margin": ["sync-macro-extensions"],
        "index-basic": ["capture-portfolio-reference-catalog"],
        "index-daily": ["sync-market-data", "capture-portfolio-reference-history"],
        "index-global": ["sync-market-data"],
        "etf-daily": ["sync-market-data", "sync-etf-heatmap"],
        "etf-shares": ["sync-etf-heatmap"],
    }
    if ident in exact:
        return exact[ident]
    if ident.startswith("macro-extension-"):
        return ["sync-macro-extensions", "recover-macro-extensions"]
    if e["category"] == "macro":
        return ["sync-macro"]
    if ident.startswith("commodity-"):
        return ["sync-commodities"]
    if ident.startswith("risk-"):
        return ["rebuild-workbench-risks", "build-and-publish"]
    if ident.startswith("card-"):
        return ["rebuild-workbench-metric-cards"]
    if ident.startswith(("screener-", "metric-")) or ident == "stock-ratio-inputs":
        return ["build-and-publish"]
    if ident in (
        "stock-repurchase",
        "stock-unlock",
        "stock-holder-count",
        "stock-top-holders",
        "stock-express",
        "stock-forecast",
        "stock-basic",
    ):
        return ["backfill-universe", "backfill"]
    return ["sync-stock-data", "backfill-universe"]


def inventory(s, root, cat, db, read):
    from app.cli.main import app as cli
    from app.main import app
    from app.local_admin.routes import mount_admin

    rels = {r["rel"]: r for r in db["relations"]}
    object_sources(root, db["relations"])
    raw = db["comparisons"]["rawTaskOwnership"]
    fields = read("docs/research/investmind-tushare-field-consumers.json")["fields"]
    index = {r["api"] + "." + r["field"]: r for r in fields}
    macro = read("docs/research/investmind-non-tushare-field-usage.json")["basicSeries"]
    items = []
    offsets = Counter()
    for e in cat:
        index_in_domain = offsets[e["category"]]
        offsets[e["category"]] += 1
        ident = e["id"]
        used = {ref for f in e["fields"] for ref in f["sourceRefs"]}
        api_names = {src.get("api") for src in e["sources"]}
        direct = {
            x["pageOrJob"]
            for name in used
            if name in index
            for x in index[name]["uses"]
            if x.get("level") == "直接"
        }
        indirect = {
            x["pageOrJob"]
            for name in used
            if name in index
            for x in index[name]["uses"]
            if x.get("level") == "间接"
        }
        if ident in macro:
            direct.update(macro[ident]["publishedFieldPaths"])
            indirect.update(macro[ident]["derivedConsumers"])
        for other in cat:
            if any(ident in f["sourceRefs"] for f in other["fields"]):
                indirect.add(other["id"])
        tables = list(dict.fromkeys(x["schema"] + "." + x["table"] for x in e["tables"]))
        for entry in e["access"]:
            if entry.get("kind") == "aggregate":
                indirect.add(entry["path"])
            elif entry.get("kind") == "independent":
                direct.add(entry["path"])
        shared = set(
            a["path"]
            for a in e["access"]
            if (a.get("path") or "").startswith("/api/v1/datasets/") and a["kind"] == "independent"
        )
        dest, reason = decision(e)
        items.append(
            {
                "id": ident,
                "name": e["name"],
                "domain": e["category"],
                "destination": dest,
                "decision": reason,
                "sharedRoutes": sorted(shared),
                "inputProviders": e["sources"],
                "fields": [f["id"] for f in e["fields"]],
                "fieldDetails": f"docs/data-catalog/{e['category']}.json#/{index_in_domain}/fields",
                "writeEntrypoints": write_paths(e),
                "declaredSchedules": e["frequencies"],
                "observedBatchDatasets": sorted({x["dataset"] for x in raw if x["api_name"] in api_names}),
                "relations": tables,
                "relationKeys": {r: rels[r]["keyChecked"] for r in tables if r in rels},
                "timeColumns": {
                    r: [
                        c["name"]
                        for c in rels[r]["columns"]
                        if c["data_type"] == "date" or c["data_type"].startswith("timestamp")
                    ]
                    for r in tables
                    if r in rels
                },
                "history": {k: v["explanation"] for k, v in e["history"].items()},
                "apis": [a for a in e["access"] if a.get("path")],
                "directConsumers": sorted(direct),
                "indirectConsumers": sorted(indirect),
                "basis": e["evidence"],
                "witness": witness(s, e, rels, raw),
            }
        )
    for r in rels.values():
        r["catalogEntries"] = [e["id"] for e in items if r["rel"] in e["relations"]]
    from app.local_admin.configuration import TASKS

    reg = runpy.run_path(str(root / "scripts/audit-scheduler-inventory.py"))
    admin = FastAPI()
    admin.state.read_engine = app.state.read_engine
    mount_admin(admin, project_root=root)
    operations = routes(app) + routes(admin.routes[-1].app, "/admin")
    cli_commands = {name: c.callback.__module__ for name, c in sorted(get_command(cli).commands.items())}
    registered_paths = {r["path"] for r in operations}
    mapped_paths = {p for item in items for p in item["sharedRoutes"]}
    if mapped_paths != {p for p in registered_paths if p.startswith("/api/v1/datasets/")}:
        raise ValueError("公共目录与注册数据集未对齐")
    return {
        "items": items,
        "routes": operations,
        "cliCommands": cli_commands,
        "adminTasks": TASKS,
        "schedulerJobs": reg["registered_jobs"](root),
        "providerConfiguration": reg["safe_config"](root),
        "byDomain": Counter(e["domain"] for e in items),
    }
