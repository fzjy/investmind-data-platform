"""只读汇总 Tushare 请求字段、原件回包、Core 键和 InvestMind 的消费证据。"""
import argparse
from collections import Counter, defaultdict
from importlib import import_module
import json
from pathlib import Path
import re
import sys

from sqlalchemy import text


KIND_API = {"income": "income", "balance": "balancesheet", "cashflow": "cashflow", "indicator": "fina_indicator"}
CORE = {
    "stock_basic":"security", "trade_cal":"trade_calendar", "disclosure_date":"disclosure_schedule",
    "daily":"daily_price", "daily_basic":"daily_basic", "adj_factor":"adjustment_factor",
    "income":"income_statement", "balancesheet":"balance_sheet", "cashflow":"cashflow_statement",
    "fina_indicator":"financial_indicator", "forecast":"financial_forecast", "express":"financial_express",
    "dividend":"dividend", "repurchase":"repurchase", "share_float":"share_unlock",
    "stk_holdernumber":"shareholder_count", "top10_holders":"top_holder", "fina_mainbz":"business_segment",
    "index_classify":"classification", "index_member_all":"classification_member",
}
ALIASES = {("stock_basic","market"):"board", ("stock_basic","list_status"):"status"}
EVIDENCE = {
    "W":"docs/research/investmind-workbench-statements.json + src/modules/stocks/workbench/financial-statement-table.tsx",
    "F":"scripts/audit-screener-fiscal-cash.py", "S":"scripts/audit-screener-capital-fields.py",
    "M":"docs/research/investmind-market-supply.json", "Q":"docs/research/investmind-workbench-quotes.json",
    "P":"app/portfolio/dividend_contract.py + app/portfolio/api", "A":"docs/research/investmind-workbench-announcements.json",
    "B":"app/stocks/business_supply.py", "G":"docs/research/investmind-macro-supply.json",
    "R":"app/stocks/marts/stocks.py::_metric_overview", "C":"app/stocks/derived/inputs.py::load_metric_card_inputs",
    "K":"app/stocks/derived/risk_inputs.py::load_risk_inputs", "T":"docs/share-unlock-field-retirement.md",
    "X":"app/market/commodities/repository.py::normalize", "L":"app/stocks/financial_supply.py::statement_tables",
    "D":"app/market/etf/repository.py", "H":"app/stocks/derived/inputs.py",
}
LEVELS = ("直接", "间接", "仅历史", "未知")
RAW_JOIN = " from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id where b.source='tushare' "


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def field_record():
    return {"contracts": set(), "rawDatasets": set(), "pages": 0, "headerOnlyPages": 0, "rawExample": None, "uses": set()}


def configured(root, catalog):
    from app.connectors.tushare.strategies import FIELD_REGISTRY, STANDARD_API_NAMES, VIP_API_NAMES
    from app.stocks.update.planning import plan_for, MARKET, FINANCIAL
    from app.macro.extensions.plan import CONTRACTS
    from app.macro.rea_input_preview import SOURCE_FIELDS, SOURCES, REA_REPORT_FIELDS
    from app.macro.balance_supplement import BalanceSupplementTarget
    from app.connectors.tushare.strategies import Dataset

    def add(api, fields, source):
        for field in fields:
            catalog[api,field]["contracts"].add(source)

    for dataset, fields in FIELD_REGISTRY.items():
        api = STANDARD_API_NAMES[dataset]
        add(api, fields, "strategies.FIELD_REGISTRY/" + dataset.value)
        if dataset in {*MARKET, *FINANCIAL} or dataset.value in ("trade_calendar","dividend","disclosure_date"):
            add(api, plan_for(dataset, {}).fields, "stocks.update.plan_for/" + dataset.value)
        if dataset in VIP_API_NAMES:
            add(VIP_API_NAMES[dataset], fields, "strategies.VIP_API_NAMES/按报告期批量")
    for row in read_json(root/"config/financial-indicators.json")["dataSupply"]["tables"].values():
        add(row["api"],row["fields"],"config/financial-indicators.json#dataSupply")
    for target in BalanceSupplementTarget:
        add("balancesheet",(*FIELD_REGISTRY[Dataset.BALANCE_SHEET],target.field),"macro.balance_supplement/"+target.value)
    for kind,fields in SOURCE_FIELDS.items():
        add(SOURCES[kind][1],(*REA_REPORT_FIELDS[kind],*fields),"macro.rea_input_preview/"+kind)
    for path, key, api in (
        ("stocks.forecast_details","EXTRA","forecast"), ("stocks.classification.contracts","MEMBER_FIELDS","index_member_all"),
        ("stocks.business_supply","FIELDS","fina_mainbz"), ("stocks.valuation_supply","FIELDS","daily_basic"),
        ("stocks.valuation_calendar","FIELDS","trade_cal"), ("market.pulse.repository","FIELDS","daily_basic"),
        ("market.region.repository","FIELDS","stock_basic"), ("portfolio.reference_catalog","FIELDS","index_basic"),
        ("portfolio.reference_history","FIELDS","index_daily"), ("portfolio.security_mapping","FIELDS","bse_mapping"),
        ("portfolio.dividend_report","DIVIDEND_FIELDS","dividend"), ("connectors.macro.tushare_dr007","FIELDS","repo_daily"),
        ("market.etf.repository","FIELDS",None), ("market.commodities.contracts","FIELDS",None),
        ("macro.target_evidence_contract","API_FIELDS",None),
    ):
        values = getattr(import_module("app."+path),key)
        for endpoint, fields in ({api:values} if api else values).items():
            add(endpoint,fields,path+"."+key)
    from app.market.data.contracts import QUOTE_FIELDS
    for row in read_json(root/"backend/config/market-data.json")["assets"]:
        add(row["apiName"],QUOTE_FIELDS,"market.data.contracts.QUOTE_FIELDS")
    macro = read_json(root/"backend/config/macro-series.json")["series"]
    for series in macro:
        for source in series["sources"]:
            if source["provider"]=="tushare":
                add(source["api"],source["responseFields"],"macro-series/"+series["id"])
                for key in source["responseFields"]:
                    if key in {source.get("field"),"trade_date","month","quarter","date"}:
                        catalog[source["api"],key]["uses"].add(("间接","宏观/指标序列 "+series["id"]+"（供应商配置，实际来源按版本）","G"))
    extra=read_json(root/"backend/config/macro-extensions.json")
    for group in ("contractSamples","groups"):
        for name,row in extra[group].items():
            if "apiName" in row:
                add(row["apiName"],row["fields"],"macro-extensions/"+group+"/"+name)
    for name,row in CONTRACTS.items():
        if row["source"]=="tushare":
            add(row["api"],row["fields"],"macro.extensions.plan.CONTRACTS/"+name)


def collect_raw(session,catalog):
    sql="""select p.api_name,b.dataset,p.payload::jsonb->'fields' as fields,count(*),
         count(*) filter (where jsonb_array_length(p.payload::jsonb->'items')=0),min(p.id)""" + RAW_JOIN + """
         and jsonb_typeof(p.payload::jsonb->'fields')='array' group by 1,2,3"""
    def observe(api, dataset, fields, pages, empty, example):
        for field in set(fields):
            row=catalog[api,field]
            row["rawDatasets"].add(dataset)
            row["pages"]+=pages
            row["headerOnlyPages"]+=empty
            row["rawExample"]=min(example,row["rawExample"] or example)
    ordinary=0
    for api,dataset,fields,pages,empty,example in session.execute(text(sql)):
        ordinary+=pages
        observe(api,dataset,fields,pages,empty,example)
    wrappers=Counter()
    errors=Counter()
    for raw_id,api,dataset,body in session.execute(text("select p.id,p.api_name,b.dataset,p.payload"+RAW_JOIN+
            "and (jsonb_typeof(p.payload::jsonb->'fields') is distinct from 'array') order by p.id")):
        if "businessData" in body or "evidence" in body:
            part=body.get("businessData",body.get("evidence",{}).get("rawResponse",{}))
            if not isinstance(part.get("fields"),list):
                raise ValueError(f"Unexpected envelope {raw_id}")
            wrappers["businessData" if "businessData" in body else "evidence.rawResponse"]+=1
            observe(api,dataset,part["fields"],1,int(not part["items"]),raw_id)
        elif api=="macro:tushare":
            base=body["request"]["source"]["api"]
            data=body.get("data") or {}
            # 一个持久化原件可覆盖多个请求窗口，按原件和字段仅计一次。
            by_api=defaultdict(dict)
            for part in data.get("requests",[]):
                endpoint=part["apiName"]
                for record in part.get("rows") or []:
                    by_api[endpoint].update(dict.fromkeys(record,True))
            for page in data.get("pages",[]):
                part=page["response"]
                for field in part["fields"]:
                    by_api[base][field]=by_api[base].get(field,False) or bool(part["items"])
            if not by_api:
                errors[dataset+"（无供应商返回字段）"]+=1
            for endpoint,fields in by_api.items():
                for field,has_rows in fields.items():
                    observe(endpoint,dataset,[field],1,int(not has_rows),raw_id)
            wrappers["macro:tushare"]+=1
        elif set(body)=={"_http"}:
            wrappers["httpOnly"]+=1
            errors[dataset+"（只有 HTTP 留证）"]+=1
        else:
            raise ValueError(f"Unrecognized provider envelope: raw.source_payload:{raw_id}")
    total=session.scalar(text("select count(*)"+RAW_JOIN))
    assert ordinary+sum(wrappers.values())==total
    return {"total":total,"standard":ordinary,"wrappers":dict(wrappers),"noResponseKeys":dict(errors)}


def published(session):
    symbols={}
    fields=defaultdict(dict)
    sql="""select product,version,payload::jsonb#>'{financial,statements,tables}' from mart.published_snapshot
           where product like 'stock-workbench:%' order by product"""
    for product,version,tables in session.execute(text(sql)):
        symbol=product.split(":",1)[1]
        symbols[symbol]=version
        for table in tables or []:
            api=KIND_API[table["kind"]]
            pending=list(table["rows"])
            while pending:
                row=pending.pop()
                pending.extend(row.get("children") or [])
                values=row["values"]
                fields[api,row["label"]][symbol]=sum(value is not None for value in values)
    if not symbols or not fields: raise ValueError("Published stock statements missing")
    return symbols,fields


def enterprise_receipts(session,catalog):
    summaries={}
    queries={}
    for subject in ("order","receivable","rea"):
        table="enterprise_"+subject
        join="a.request_key=p.request_key" if subject=="rea" else "a.id=p.attempt_id"
        queries["raw."+table+"_response"]=(
            f"select a.request::jsonb->>'apiName',p.request_key,p.payload from raw.{table}_response p "
            f"join ops.{table}_attempt a on {join}")
    queries["raw.enterprise_target_observation"]=(
        "select api_name,observation_id,response from raw.enterprise_target_observation where provider='tushare'")
    for table,sql in queries.items():
        if session.scalar(text("select to_regclass(:table)"),{"table":table}) is None:
            summaries[table]=None
            continue
        values=session.execute(text(sql))
        count=0
        for api,key,body in values:
            count+=1
            for field in body["fields"]:
                stores=catalog[api,field].setdefault("enterpriseRaw",{})
                evidence=stores.setdefault(table,{"pages":0,"headerOnly":0,"example":key})
                evidence["example"]=min(evidence["example"],key)
                evidence["pages"]+=1
                evidence["headerOnly"]+=int(not body["items"])
        summaries[table]=count
    return summaries


def signature_fields(factory,session,catalog,symbols):
    from app.stocks.update.publication import StockSnapshotRepository
    from sqlalchemy import inspect
    day=session.scalar(text("select min(as_of)::date from mart.published_snapshot where product like 'stock-workbench:%'"))
    snap=StockSnapshotRepository(factory).load(day,tuple(symbols))
    # 对已载入生产签名的对象逐行取真实键。具名列有值或 JSON 中存在该键才登记来源。
    represented=defaultdict(set)
    source_map={"securities":"stock_basic","daily_prices":"daily","daily_basics":"daily_basic",
                "adjustment_factors":"adj_factor","trade_calendar":"trade_cal","classifications":"index_classify"}
    table_to_api={table:api for api,table in CORE.items()}
    for name in (*source_map,"corporate_actions","classification_members"):
        for obj in getattr(snap,name):
            api=source_map.get(name) or table_to_api[obj.__table__.name]
            keys=represented[api]
            keys.update(k for k in getattr(obj,"data",{}) or {} if (api,k) in catalog)
            keys.update(c.key for c in inspect(type(obj)).columns if (api,c.key) in catalog and getattr(obj,c.key) is not None)
    for point in snap.financial_points:
        api=table_to_api[point.source_table.split(".")[1]]
        represented[api].update(field for field in point.values if (api,field) in catalog)
    for api,fields in represented.items():
        for field in fields:
            catalog[api,field]["uses"].add(("间接","股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）",
                                            "app/stocks/update/publication.py::source_signature"))
    return {"asOf":day.isoformat(),"fieldsInSignature":sum(map(len,represented.values()))}


def core_fields(session):
    # 区分真实有值的列、JSON 键与仅有模型定义。SQL 标识符只来自固定表名和数据库目录。
    result=defaultdict(dict)
    for table in sorted(set(CORE.values())):
        columns=list(session.scalars(text("""select column_name from information_schema.columns
            where table_schema='core' and table_name=:table order by ordinal_position"""),{"table":table}))
        if not columns or not all(re.fullmatch(r"[a-z_][a-z_0-9]*",name) for name in columns):
            raise ValueError(table)
        selected=[name for name in columns if name!="data"]
        counts=session.execute(text("select "+",".join(f'count("{name}")' for name in selected)+f" from core.{table}")).one()
        for name,n in zip(selected,counts,strict=True):
            result[table][name]=n
        if "data" in columns:
            for key,n in session.execute(text(f"""select kv.key,count(*) filter(where kv.value<>'null'::jsonb)
                from core.{table} c cross join lateral jsonb_each(c.data::jsonb) kv group by kv.key""")):
                result[table]["data."+key]=n
    return result


def conditional_core_paths(root,session,catalog):
    """记录同一 API 仅在特定转换和数据集内成立的落库路径。"""
    from app.market.data.contracts import QUOTE_FIELDS
    for api,kind,rows in session.execute(text("""
        select q.api_name,q.asset_type,count(*) from core.market_daily_quote q
        join raw.source_payload p on p.id=q.source_payload_id
        join raw.ingestion_batch b on b.id=p.batch_id
        where q.provider='tushare' and p.api_name=q.api_name and b.source='tushare'
        and b.dataset='market:'||q.asset_type||':'||q.asset_code
        group by 1,2 order by 1,2""")):
        for field in QUOTE_FIELDS:
            column="asset_code" if field=="ts_code" else field
            catalog[api,field].setdefault("conditionalCore",[]).append({
                "column":"core.market_daily_quote."+column,
                "condition":"market:"+kind+":资产代码；证券代码与配置身份校验",
                "linkedRows":rows})
    macro_counts=dict(session.execute(text("""select series_id,count(*) from core.macro_observation
        where source='tushare' group by series_id""")).all())
    for series in read_json(root/"backend/config/macro-series.json")["series"]:
        for source in series["sources"]:
            if source["provider"]!="tushare":
                continue
            for field,column in ((source["responseFields"][0],"observed_at"),(source["field"],"value")):
                catalog[source["api"],field].setdefault("conditionalCore",[]).append({
                    "column":"core.macro_observation."+column,
                    "condition":"系列 "+series["id"]+" 的 Tushare 报告期或数值，经转换与去重/修订",
                    "seriesRows":macro_counts.get(series["id"],0)})

def add_consumers(root,catalog,statements):
    def record(api,fields,level,path,proof):
        for field in fields.split() if isinstance(fields,str) else fields:
            if (api,field) in catalog:
                catalog[api,field]["uses"].add((level,path,proof))

    for (api,field),witness in statements.items():
        if (api,field) not in catalog: continue  # 排除网站派生的毛利与自由现金流等。
        record(api,[field],"直接",f"工作台/财务报表/financial.statements.tables.rows[{field}]", "W")
        catalog[api,field]["publishedStatement"]={
            "symbols":len(witness),"nonNullSymbols":sum(n>0 for n in witness.values()),"nonNullCells":sum(witness.values()),
            "example":next((s for s,n in sorted(witness.items()) if n),min(witness))}
    for spec in read_json(root/"config/stock-ratio-catalog.json")["fields"]:
        if spec.get("table") not in KIND_API: continue
        record(KIND_API[spec["table"]],spec["keys"],"间接","工作台/比率分析/metrics.fields["+spec["id"]+"]（按别名顺序取首个有效值）","R")
    from app.stocks.derived.inputs import CARD_REPORT_FIELDS
    from app.stocks.derived.risk_inputs import FIELDS as RISK_INPUTS
    for collection,page,proof in [(CARD_REPORT_FIELDS,"工作台/估值与财务指标卡的报告取版","C"),(RISK_INPUTS,"工作台/风险面板的报告筛选及计算","K")]:
        for kind,fields in collection.items():
            record(KIND_API[kind],fields,"间接",page,proof)
    ratios={
        "n_cashflow_act":("cashflow","realMoneyIndex, ocfToRevenue, ocfToTotalLiab, capexCoverageRatio, freeCashFlowFy, freeCashFlowMarginFy"),
        "c_pay_acq_const_fiolta":("cashflow","capexCoverageRatio, freeCashFlowFy, freeCashFlowMarginFy"),
        "c_fr_sale_sg":("cashflow","cashCollectionRatio"),"c_paid_goods_s":("cashflow","cashPaymentRatio"),
        "revenue":("income","ocfToRevenue, cashCollectionRatio, freeCashFlowMarginFy"),
        "n_income_attr_p":("income","realMoneyIndex"),"oper_cost":("income","cashPaymentRatio"),"total_liab":("balancesheet","ocfToTotalLiab"),
    }
    for field,(api,metrics) in ratios.items():
        record(api,[field],"间接","选股/"+metrics+"（FY、3/5/10 年）","F")
    for api,fields,level,use,proof in [
        ("daily","ts_code trade_date open high low close vol amount pct_chg","直接","工作台/行情与日 K；市场/成交、涨跌","Q"),
        ("daily","close","间接","选股/预期、NTM 与财年股息率；组合/持仓行情","S"),
        ("daily","pre_close","直接","工作台/昨收","Q"),
        ("adj_factor","ts_code trade_date adj_factor","间接","工作台/日 K 复权序列","Q"),
        ("stock_basic","ts_code name","直接","搜索、选股、工作台、市场、组合/证券代码及名称","Q"),
        ("stock_basic","area","直接","市场/地区热力分类","M"),
        ("stock_basic","list_date exchange","间接","股票任务/上市日期与交易所限定预期交易日","app/stocks/update/planning.py"),
        ("trade_cal","exchange cal_date is_open","间接","股票与市场/发现业务日并对齐行情窗口","app/stocks/update/planning.py"),
        ("daily_basic","pe pb total_mv pe_ttm","直接","选股、工作台/静态 PE、PB、市值、滚动 PE 各自对应同名口径","Q"),
        ("daily_basic","pe pb","间接","选股/pePercentile、pbPercentile；银行长期估值","S"),
        ("daily_basic","total_share float_share","间接","选股/持仓占比；工作台/证券股本输入","H"),
        ("daily_basic","limit_status","间接","市场/涨跌停家数","M"),
        ("fina_indicator","roe roic","直接","选股/ROE 最近报告、ROIC 最近有效 FY","S"),
        ("dividend","record_date ex_date pay_date div_proc cash_div cash_div_tax end_date ann_date","间接","选股、工作台、组合/实施版本、财年归属、股息到账及收益率","P"),
        ("dividend","stk_div stk_bo_rate stk_co_rate base_date base_share imp_ann_date","间接","组合/现金及股本事件去重、基数与实施公告","P"),
        ("repurchase","proc amount","间接","选股/资本回报与股权筹资比","S"),
        ("cashflow","c_recp_cap_contrib","间接","选股/capitalReturnRatio 及累计窗口","S"),
        ("income","n_income_attr_p","间接","选股/payoutRatio","S"),
        ("stk_holdernumber","holder_num","直接","工作台/股东户数","docs/research/investmind-workbench-panels.json"),
        ("top10_holders","holder_name holder_type hold_amount hold_ratio","间接","选股/institutionHolding、institutionHoldingChange","S"),
        ("bse_mapping","o_code n_code","间接","组合/分红证据中新旧证券代码归并","app/portfolio/security_mapping.py"),
        ("fina_mainbz","bz_item bz_sales bz_profit bz_cost curr_type","间接","工作台/主营构成，按产品/地区分类、币种核对","B"),
        ("index_member_all","l1_code l2_code l3_code l1_name l2_name l3_name ts_code in_date out_date","间接","选股、市场/证券有效行业归属与板块聚合","M"),
        ("index_classify","index_code industry_name parent_code level industry_code","间接","选股、市场/分类树与行业标签","M"),
        ("disclosure_date","end_date actual_date ts_code","间接","选股、工作台/已披露期与补采","app/stocks/update/planning.py"),
        ("forecast","summary change_reason type net_profit_min net_profit_max p_change_min p_change_max","直接","工作台/公告与业绩预告明细","A"),
        ("fund_share","fd_share","直接","市场/ETF 份额；参与基金规模换算","D"),
        ("fund_daily","close pct_chg amount","直接","市场/ETF 收盘、涨跌与成交","D"),
        ("fut_mapping","mapping_ts_code","间接","市场/商品主力合约及换月","M"),
        ("fut_daily","open high low close vol amount oi","直接","市场/商品日 K，周月聚合","M"),
        ("fut_daily","pre_close pre_settle settle","间接","市场/商品主力日行情完整性校验","X"),
        ("fut_trade_cal","exchange cal_date is_open","间接","市场/商品开市日选择","X"),
        ("fut_basic","ts_code fut_code exchange","间接","市场/商品月合约目录与交易所校验","X"),
        ("index_daily","close","间接","组合/基准回报","app/portfolio/reference_history.py"),
        ("index_basic","ts_code fullname","间接","组合/全收益基准目录与目标名称","app/portfolio/reference_history.py"),
        ("share_float","float_share ts_code ann_date float_date","间接","股票发布/公司行为可见性与来源签名","T"),
        ("margin","rzye exchange_id trade_date","间接","宏观/融资余额分交易所核验与汇总","G"),
        ("daily_info","float_mv exchange ts_code trade_date","间接","宏观/A 股流通市值交易所详细源","G"),
        ("cn_gdp","gdp gdp_yoy quarter","间接","宏观/名义 GDP、实际同比诊断、季度","G"),
        ("fx_daily","bid_close ask_close ts_code exchange trade_date","间接","宏观/离岸汇率报价方向、时点和身份","G"),
        ("repo_daily","weight repo_maturity ts_code trade_date","间接","宏观/DR007 利率与品种、日期校验","G"),
        ("daily_basic","ts_code trade_date","间接","工作台/证券交易日关联行情与估值","C"),
        ("daily_basic","dv_ttm dv_ratio","间接","股票发布/股息率输入与备选","H"),
        ("daily_basic","ps_ttm ps","间接","工作台/PS 估值带数据优先级","app/stocks/workbench_enrichment.py::valuation_bands"),
        ("fina_indicator","grossprofit_margin netprofit_margin","直接","选股/毛利率、净利率","app/stocks/marts/stocks.py"),
        ("fina_indicator","tax_to_ebt invest_capital","间接","股票派生/税后营业利润、投入资本原料","H"),
        ("disclosure_date","ann_date modify_date pre_date","间接","股票任务/披露日历修订比较","app/stocks/update/repository.py"),
        ("dividend","ts_code div_listdate","间接","组合/证券匹配与送转到账日","app/portfolio/dividend_ledger.py"),
        ("forecast","ts_code end_date ann_date first_ann_date","间接","工作台/预告详情补充、版本匹配","app/ingestion/repositories.py"),
        ("fund_daily","vol","间接","市场/ETF 成交量非负核验","D"),
        ("fund_share","ts_code trade_date","间接","市场/ETF 份额证券与行情同日核验","D"),
        ("fut_mapping","ts_code trade_date","间接","市场/主力映射与开市日逐日对齐","X"),
        ("fut_daily","ts_code trade_date","间接","市场/合约行情对应主力与日期","X"),
        ("fut_basic","list_date delist_date quote_unit trade_unit per_unit","间接","市场/商品有效合约区间、报价和交易单位","X"),
        ("fina_mainbz","ts_code end_date update_flag","间接","工作台/主营业务来源证券、报告期及修订校验","B"),
        ("index_member_all","is_new","间接","选股、市场/分类数据集当前与历史有效区间","app/stocks/classification/contracts.py"),
        ("index_basic","market publisher","间接","组合/全收益目录身份校验","app/portfolio/reference_history.py"),
        ("stock_basic","symbol","间接","搜索、组合/证券简称拼音备选、证券编号","app/stocks/marts/stocks.py"),
        ("stock_basic","list_status","间接","股票发布/证券可见和退市校验","H"),
        ("top10_holders","ts_code end_date ann_date","间接","选股、工作台/按证券与可见日期取股东名册","H"),
        ("stk_holdernumber","ts_code end_date ann_date","间接","工作台/股东户数期末及公告取版","docs/research/investmind-workbench-panels.json"),
        ("trade_cal","pretrade_date","间接","宏观/交易所汇总的前一交易日证据","app/macro/exchange_summary_store.py"),
        ("sz_daily_info","ts_code trade_date count float_mv","间接","宏观/深市统计样本合同与数据类型；单位待验，未晋升正式序列","app/macro/sz_market_sample.py"),
        ("balancesheet","accounts_receiv contract_liab","间接","宏观/企业应收与订单补充见证","app/macro/receivables_match.py"),
        ("fut_basic","name","间接","市场/商品目录整列参与请求指纹、回包字段集合和游标哈希复验；页面名称来自资产配置","app/market/commodities/repository.py::read"),
        ("index_basic","base_date list_date desc","间接","组合/两项全收益基准旧目录的请求哈希与不可变原件校验；基准筛选不读这三个值","app/portfolio/reference_history.py"),
        ("index_member_all","name","间接","市场与选股/行业成员原件全包哈希与合同指纹；页面显示证券名来自证券主表","app/stocks/classification/repository.py::_response"),
        ("fina_mainbz","segment_name","间接","通用主营归一化/旧格式 Core 记录的更新身份；当前来自供应商的同名值未取证，现行工作台用 bz_item","app/ingestion/pipeline.py"),
    ]:
        record(api,fields,level,use,proof)
    from app.macro.rea_input_preview import SOURCE_FIELDS
    from app.macro.target_evidence_contract import API_FIELDS
    for kind,keys in SOURCE_FIELDS.items():
        record(KIND_API[kind],keys,"间接","宏观/企业 REA 五节点补充见证与取版","app/macro/rea_input_preview.py")
    for api,fields in API_FIELDS.items():
        record(api,fields,"间接","宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围",
               "app/macro/target_evidence_normalize.py")
    for api in ("index_daily","index_global","fund_daily"):
        record(api,"open high low close pre_close pct_chg trade_date ts_code","间接","市场/指数或 ETF 同日行情和产品校验","M")
    market_quote_evidence = "app/market/data/publication.py + scripts/audit-market-supply.py"
    for api in ("index_daily","index_global"):
        for field,target in (("trade_date","tradeDate 收盘交易日"),("pct_chg","change 涨跌幅"),
                             ("close","value 收盘点位（两位小数）")):
            record(api, [field], "直接", "市场总览/全球指数卡/globalIndices[]." + target,
                   market_quote_evidence + " + src/modules/market/index-card.tsx")
    for field,target in (("trade_date","tradeDate 收盘交易日"),("pct_chg","change 涨跌幅")):
        record("fund_daily", [field], "直接", "市场总览/宽基 ETF 卡/etfQuotes[]." + target,
               market_quote_evidence + " + src/modules/market/market-overview.tsx")
    for api in ("income","balancesheet","cashflow"):
        record(api,"ts_code ann_date f_ann_date end_date report_type update_flag","间接","工作台/可见报告期、证券与修订取版","L")
    record("balancesheet","comp_type","间接","工作台/适用行业 companyType","L")
    catalog["share_float","unlock_date"]["uses"].add(("仅历史","旧宽合同/新请求已删除，历史回放保留；当次历史没有返回该键","T"))
    from app.stocks.jobs.daily_sync_plans import FINANCIAL_DATASETS
    from app.stocks.jobs.universe_backfill import STOCK_DATASETS
    from app.connectors.tushare.strategies import Dataset
    if Dataset.FINANCIAL_EXPRESS in (*FINANCIAL_DATASETS,*STOCK_DATASETS):
        raise ValueError("express remains in default collection")
    if Dataset.BUSINESS_SEGMENT not in FINANCIAL_DATASETS or Dataset.BUSINESS_SEGMENT not in STOCK_DATASETS:
        raise ValueError("generic business compatibility missing")
    for (api,field),entry in catalog.items():
        if api=="express":
            entry["uses"].add(("仅历史","业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留","app/stocks/jobs/daily_sync_plans.py + app/stocks/jobs/universe_backfill.py"))


def render(catalog, raw, symbols, core):
    rows=[]
    for (api,field),entry in sorted(catalog.items()):
        base=api.removesuffix("_vip")
        table=CORE.get(base)
        lookup=core.get(table,{})
        actual={}
        # Core 实测按表；同名 VIP 和标准 API 共用目标，不将标准端点的值归到 VIP 上。
        if table and api==base:
            for name in (field,ALIASES.get((api,field)),"data."+field):
                if name in lookup:
                    actual["core."+table+"."+name]=lookup[name]
        uses=sorted(entry["uses"])
        level=next((level for level in LEVELS if any(item[0]==level for item in uses)),"未知")
        if api.endswith("_vip") and not entry["pages"]:
            uses=[]; level="未知"
        rows.append({"api":api,"field":field,"level":level,"contracts":sorted(entry["contracts"]),
                     "rawPages":entry["pages"],"headerOnlyPages":entry["headerOnlyPages"],"rawExampleId":entry["rawExample"],
                     "datasets":sorted(entry["rawDatasets"]),"enterpriseRaw":entry.get("enterpriseRaw",{}),
                     "conditionalCore":entry.get("conditionalCore",[]),
                     "coreDatasetTable":"core."+table if table else None,
                     "vipStandardField":base+"."+field if base!=api else None,
                     "vipPath":("按需显式回填；不参加默认计划" if api=="express_vip" else "具备权限且选择 VIP 模式时按报告期批量") if base!=api else None,
                     "coreNonNullCounts":actual,"publishedStatement":entry.get("publishedStatement"),
                     "uses":[{"level":l,"project":"InvestMind","pageOrJob":path,"evidence":proof} for l,path,proof in uses]})
    counts=Counter(row["level"] for row in rows)
    return {"scope":"Tushare 当前请求契约、库内留存的真实回包字段与已登记的历史退役键；供应商、任务及业务库写入均为零",
            "count":len(rows),"apis":len({r["api"] for r in rows}),"levels":dict(counts),
            "configured":sum(bool(r["contracts"]) for r in rows),"rawObserved":sum(bool(r["rawPages"] or r["enterpriseRaw"]) for r in rows),
            "publishedStatementFields":sum(bool(r["publishedStatement"]) for r in rows),
            "workbenches":len(symbols),"publicationVersions":dict(Counter(symbols.values())),
            "receipts":raw,"evidence":EVIDENCE,"fields":rows}


def markdown(report,project=False):
    lines=["# Tushare 原字段到 InvestMind 消费者","","每行标识一个接口的一个响应字段。对于同名字段，须连同接口搜索。直接和间接用途可并存，汇总优先显示直接。未知项保留采集状态，已确认退役的旧合同保留历史。全量机器可读回执包含来源契约、Raw 示例 ID、Core 实测非空行数及各个页面或任务的证据。","",
           "[完整逐字段回执](research/investmind-tushare-field-consumers.json) · [只读重放脚本](../scripts/audit-tushare-field-consumers.py)","",
           f"本次 {report['apis']} 个端点共 {report['count']} 项，其中 {'、'.join(f'{k} {report['levels'].get(k,0)}' for k in LEVELS)}。{report['configured']} 项存在于登记的现行或离线契约，{report['rawObserved']} 项有真实返回列。{report['workbenches']} 个发布工作台中出现 {report['publishedStatementFields']} 项同名来源报表行。","",
           "## 如何读表","","- Raw 数是包含该返回列的留存原件数，空数组保留表头也计数。JSON 中可分开查看仅表头原件数。宏观窗口按原件去重。HTTP 错误体和只有请求配置的空响应不提供实际字段见证。",
           "- Core 对应数据集表为标准归一化路径。同一字段可在普通列或 data JSON 中存在。回执按全表统计实际键，无法据同名键推定某条记录来自某个调用。三表专题直接从 Raw 取版，现金流还能合并 Core。专题数据可能绕过常规 Core 落库，实际数据集及原件 ID 可区分路径。",
           "- VIP 批量端点独立统计。未采到对应端点的原件时，查同名标准端点可了解下游设计，当前 VIP 来源到生产页面的关系仍待实证。",
           "- 已发布动态行区分存在和有值。原件到财报单元格的逐期校验见工作台三表专题。静态公式和别名表证明代码会读取，未逐项目证明每个备选在某日实际命中。可能还存在未命名的动态消费者或其他项目。","",
           "## 来源字段","","| 接口.字段 | 归一化数据集或专题原件 | Raw 数 | 状态 | 已登记用途 |","| --- | --- | ---: | --- | --- |"]
    for row in report["fields"]:
        path=row["coreDatasetTable"] or ("raw.source_payload" if row["rawPages"] else "")
        if row["enterpriseRaw"]:path+=" / "+", ".join(sorted(row["enterpriseRaw"]))
        path=path.strip(" /") or "待取得原件"
        if any(dataset=="financial-statement-evidence" for dataset in row["datasets"]):path+=" / 财报专题 Raw"
        if any(d.startswith(("macro:","macro-extension:","macro-exchange-summary:")) for d in row["datasets"]):path+=" / 宏观观测按发布合同"
        for branch in row["conditionalCore"]:
            count=branch.get("seriesRows",branch.get("linkedRows"))
            path+=f" / {branch['column']}（{branch['condition']}，{count} 行）"
        usage="；".join(u["pageOrJob"] for u in row["uses"]) or "待核"
        pages=row['rawPages']+sum(item['pages'] for item in row['enterpriseRaw'].values())
        lines.append(f"| `{row['api']}.{row['field']}` | {path} | {pages} | {row['level']} | {usage} |")
    if project:
        lines[0]="# InvestMind 使用的 Tushare 原字段"
        lines[4]="[页面字段与使用位置](field-consumers.md) · [按项目模块查数据链路](investmind-lineage.md)。此目录由独立数据底座的 `scripts/audit-tushare-field-consumers.py --project-markdown` 根据同一次只读核验生成；原件编号和逐表计数见底座 `docs/research/investmind-tushare-field-consumers.json`。"
    extras=[
        "## 默认采集与历史兼容","",
        "- 业绩快报 `express` 的七个字段没有出现在发布或来源签名代码。新日增量、全池回填（标准和 VIP）均停止规划快报。已有原件、Core、显式按数据集回填和旧失败项的显式重放保留。底座不删除已有行。",
        "- 通用主营合同 `fina_mainbz.revenue/segment_type/segment_name` 与专题的 `bz_sales/bz_item` 分开核对。三个通用字段均没有实际返回列或同名 Core 非空值。旧格式的 `segment_name` 参与写入身份，旧格式整行可进入财务来源签名。当前不具备安全移除通用合同的证据。主营专题继续按财报期和产品/地域采集。",
        "- `fut_basic.name`、`index_basic.base_date/list_date/desc`、`index_member_all.name` 参与已有原件的完整字段合同、请求键或游标哈希复核。没有找到直接展示这五个值的页面；每行列明用到该合同的任务。收窄合同需区分旧版重放与新版请求，再验证商品、全收益基准和申万成员三个持久化读取器。",
        "- 7 个 VIP 端点映射到同名标准端点，合计 146 个字段与标准端点逐一同名。当前本地模式和原件记录在回执；无 VIP 响应见证。相同字段的标准端点消费关系可按对应端点反查。显式选择 VIP 且账号具备权限才可按报告期取数；快报默认停采，显式回填可单独选择。未向供应商探测账号实际权限。",
        "- 现金流、应收与订单等企业补充独立写入 `raw.enterprise_*_response`，指数候选写入 `raw.enterprise_target_observation`。这些表的字段回执与通用原件分列；目标候选并不自动进入网站展示。",
        "",
    ]
    lines[lines.index("## 来源字段"):lines.index("## 来源字段")]=extras
    return "\n".join(lines)+"\n"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json",type=Path)
    parser.add_argument("--markdown",type=Path)
    parser.add_argument("--project-markdown",type=Path,help="另写网站侧的逐字段查询目录")
    args=parser.parse_args()
    root=args.root.resolve()
    sys.path.insert(0,str(root/"backend"))
    from app.config import settings
    from app.shared.readonly_database import open_readonly_factory
    catalog=defaultdict(field_record)
    configured(root,catalog)
    with open_readonly_factory(settings.database_read_url) as factory,factory() as session:
        assert session.scalar(text("show transaction_read_only"))=="on"
        raw=collect_raw(session,catalog)
        raw["enterpriseTables"]=enterprise_receipts(session,catalog)
        raw["localTushareMode"]=settings.tushare_mode
        raw["expressHistory"]=dict(session.execute(text("""select count(*) as rows,count(distinct ts_code) as symbols,
            min(end_date)::text as first_period,max(end_date)::text as last_period,
            min(ann_date)::text as first_announcement,max(ann_date)::text as last_announcement from core.financial_express""")).mappings().one())
        symbols,statements=published(session)
        core=core_fields(session)
        conditional_core_paths(root,session,catalog)
        raw["publicationSignature"]=signature_fields(factory,session,catalog,symbols)
    add_consumers(root,catalog,statements)
    report=render(catalog,raw,symbols,core)
    if report["rawObserved"]==0 or report["publishedStatementFields"]==0: raise ValueError("Empty inventory")
    json_path=args.json or root/"docs/research/investmind-tushare-field-consumers.json"
    md_path=args.markdown or root/"docs/tushare-field-consumers.md"
    json_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    md_path.write_text(markdown(report),encoding="utf-8")
    if args.project_markdown:
        args.project_markdown.write_text(markdown(report,project=True),encoding="utf-8")
    print(json.dumps({key:value for key,value in report.items() if key not in {"fields","evidence"}},ensure_ascii=True))


if __name__=="__main__":
    main()


