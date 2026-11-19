"""离线整理数据目录。维护中文语义后显式运行，生成的 JSON 纳入版本控制。"""
from __future__ import annotations
from collections import defaultdict
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"backend"), str(ROOT/"packages/news-collector/src"), str(ROOT/"scripts")]
from app.local_admin.catalog_inventory import identities, read_json, source_contracts
from app.connectors.tushare.strategies import FIELD_REGISTRY, STANDARD_API_NAMES, VIP_API_NAMES
from app.stocks.derived.registry import FORMULAS
from app.stocks.derived.workbench_cards import DEFINITIONS, CARD_VERSION
from app.stocks.derived.workbench_risks import RULES, VERSION

LABELS = {}
for line in """
ts_code=证券代码|symbol=证券代码|name=名称|exchange=交易所|market=市场|area=地区|industry=行业|fullname=公司全称|enname=英文名称|cnspell=拼音缩写|list_status=上市状态|list_date=上市日期|delist_date=退市日期|is_hs=沪深港通标识|act_name=实际控制人|act_ent_type=控制人企业性质|curr_type=币种
trade_date=交易日期|cal_date=日历日期|is_open=是否开市|pretrade_date=上个交易日|open=开盘价|high=最高价|low=最低价|close=收盘价|pre_close=前收盘价|change=涨跌额|pct_chg=涨跌幅|vol=成交量|amount=成交额|adj_factor=复权因子|turnover_rate=换手率|turnover_rate_f=自由流通换手率|volume_ratio=量比|pe=市盈率|pe_ttm=滚动市盈率|pb=市净率|ps=市销率|ps_ttm=滚动市销率|dv_ratio=股息率|dv_ttm=滚动股息率|total_share=总股本|float_share=流通股本|free_share=自由流通股本|total_mv=总市值|circ_mv=流通市值|limit_status=涨跌停状态
ann_date=公告日期|f_ann_date=实际公告日期|end_date=报告期末|report_type=报表类型|comp_type=公司类型|update_flag=更新标识|actual_date=实际披露日期|pre_date=预计披露日期|modify_date=修正日期|quarter=季度|year=年度|month=月份|date=日期|value=数值|unit=单位|source=数据来源|title=标题|url=来源链接
revenue=营业收入|total_revenue=营业总收入|oper_cost=营业成本|total_cogs=营业总成本|sell_exp=销售费用|admin_exp=管理费用|rd_exp=研发费用|fin_exp=财务费用|operate_profit=营业利润|total_profit=利润总额|biz_tax_surchg=税金及附加|income_tax=所得税费用|n_income=净利润|n_income_attr_p=归母净利润|minority_gain=少数股东损益|invest_income=投资收益|fv_value_chg_gain=公允价值变动收益|non_oper_income=营业外收入|non_oper_exp=营业外支出|assets_impair_loss=资产减值损失|credit_impa_loss=信用减值损失|t_compr_income=综合收益总额|basic_eps=基本每股收益|diluted_eps=稀释每股收益|ebit=息税前利润|ebitda=息税折旧摊销前利润|fin_exp_int_exp=利息费用|fin_exp_int_inc=利息收入
total_assets=总资产|total_liab=总负债|total_hldr_eqy_exc_min_int=归母股东权益|total_hldr_eqy_inc_min_int=股东权益合计|money_cap=货币资金|trad_asset=交易性金融资产|notes_receiv=应收票据|accounts_receiv=应收账款|prepayment=预付款项|oth_receiv=其他应收款|inventories=存货|contract_assets=合同资产|total_cur_assets=流动资产合计|total_nca=非流动资产合计|fix_assets=固定资产|intan_assets=无形资产|goodwill=商誉|st_borr=短期借款|lt_borr=长期借款|bond_payable=应付债券|st_bonds_payable=短期应付债券|lease_liab=租赁负债|non_cur_liab_due_1y=一年内到期非流动负债|total_cur_liab=流动负债合计|total_ncl=非流动负债合计|acct_payable=应付账款|notes_payable=应付票据|adv_receipts=预收款项|contract_liab=合同负债|payroll_payable=应付职工薪酬|taxes_payable=应交税费|oth_payable=其他应付款|lt_eqt_invest=长期股权投资|minority_int=少数股东权益|oth_cur_assets=其他流动资产|oth_cur_liab=其他流动负债|oth_nca=其他非流动资产|undistr_porfit=未分配利润|cap_rese=资本公积|surplus_rese=盈余公积
n_cashflow_act=经营活动现金流净额|n_cashflow_inv_act=投资活动现金流净额|n_cash_flows_fnc_act=筹资活动现金流净额|c_pay_acq_const_fiolta=购建长期资产支付现金|c_recp_sell_goods=销售商品收到现金|c_pay_goods_services=购买商品支付现金|c_pay_to_for_empl=支付给职工的现金|c_pay_dist_dpcp_int_exp=分配股利利润或偿息支付现金|c_recp_cap_contrib=吸收投资收到现金|c_cash_equ_end_period=期末现金及现金等价物|free_cashflow=自由现金流|n_incr_cash_cash_equ=现金及现金等价物净增加额|c_cash_equ_beg_period=期初现金及现金等价物|c_recp_borrow=取得借款收到现金|c_prepay_amt_borr=偿还债务支付现金
eps=每股收益|dt_eps=扣非每股收益|roe=净资产收益率|roe_dt=扣非净资产收益率|roe_yearly=年化净资产收益率|roic=投入资本回报率|roa=资产回报率|grossprofit_margin=毛利率|netprofit_margin=净利率|debt_to_assets=资产负债率|current_ratio=流动比率|quick_ratio=速动比率|cash_ratio=现金比率|bps=每股净资产|ocfps=每股经营现金流|revenue_ps=每股营业收入|capital_rese_ps=每股资本公积|undist_profit_ps=每股未分配利润|netprofit_yoy=归母净利润同比|or_yoy=营业收入同比|op_yoy=营业利润同比|tr_yoy=营业总收入同比|q_netprofit_yoy=单季归母净利润同比|q_sales_yoy=单季营业收入同比
div_proc=分红进度|cash_div_tax=每股税前现金分红|cash_div=每股现金分红|stk_div=每股送转股数|stk_bo_rate=每股送股比例|stk_co_rate=每股转增比例|imp_ann_date=实施公告日期|record_date=股权登记日|ex_date=除权除息日|pay_date=派息日|div_listdate=红股上市日|base_date=分红基准日|base_share=分红基准股本|proc=实施进度|exp_date=计划截止日期|vol_lower=回购数量下限|vol_upper=回购数量上限|high_limit=价格上限|low_limit=价格下限
holder_name=股东名称|hold_amount=持股数量|hold_ratio=持股比例|hold_float_ratio=流通股持股比例|holder_type=股东类型|holder_num=股东户数|float_date=解禁日期|float_ratio=解禁比例|share_type=股份类型|bz_item=主营分部项目|bz_sales=主营业务收入|bz_profit=主营业务利润|bz_cost=主营业务成本|segment_name=分部名称|segment_type=分部类型|type=类型
p_change_min=净利润变动下限|p_change_max=净利润变动上限|net_profit_min=净利润下限|net_profit_max=净利润上限|last_parent_net=上期归母净利润|first_ann_date=首次公告日期|summary=摘要|change_reason=变动原因|diluted_roe=摊薄净资产收益率|yoy_net_profit=净利润同比|yoy_sales=营业收入同比
index_code=指数代码|index_name=指数名称|con_code=成分证券代码|weight=权重或加权利率|in_date=纳入日期|out_date=剔除日期|is_new=是否最新|level=分类层级|industry_code=行业代码|industry_name=行业名称|parent_code=父级分类代码|src=分类标准|publisher=发布机构|category=类别|base_date=基日|base_point=基点|desc=描述|exp_return=预期收益率
fund_type=基金类型|found_date=成立日期|due_date=到期日期|maturity_date=到期日期|invest_type=投资类型|management=管理人|custodian=托管人|benchmark=业绩比较基准|fund_share=基金份额|fund_id=基金代码|etf_type=ETF类别|fund_name=基金名称|fund_name_abbr=基金简称
fut_code=期货品种代码|fut_name=期货名称|fut_type=期货类型|contract=合约|d_month=交割月份|last_ddate=最后交割日|multiplier=合约乘数|trade_unit=交易单位|per_unit=每手单位|quote_unit=报价单位|quote_unit_desc=报价说明|d_mode_desc=交割方式|fut_mapping_ts_code=映射合约代码|mapping_ts_code=对应合约代码|pre_settle=昨结算价|settle=结算价|oi=持仓量|oi_chg=持仓变化|delivery=交割
bid_open=买入开盘价|bid_close=买入收盘价|bid_high=买入最高价|bid_low=买入最低价|ask_open=卖出开盘价|ask_close=卖出收盘价|ask_high=卖出最高价|ask_low=卖出最低价|tick_qty=报价次数
gdp=累计名义国内生产总值|gdp_yoy=国内生产总值同比|nt_yoy=全国居民消费价格同比|ppi_yoy=工业生产者出厂价格同比|pmi010000=制造业采购经理指数|m1_yoy=狭义货币同比|m2_yoy=广义货币同比|rzye=融资余额|rqye=融券余额|rzmre=融资买入额|rzche=融资偿还额|rqmcl=融券卖出量|rqyl=融券余量|rzrqye=融资融券余额|rqchl=融券偿还量|y10=十年期收益率|float_mv=流通市值|count=数量|total_market=总市值|float_market=流通市值
cfo_ttm=近十二个月经营现金流|parent_profit_ttm=近十二个月归母净利润|capex_ttm=近十二个月资本开支|nopat=税后经营利润|invested_capital=投入资本|pe_history_3y=三年市盈率样本|pb_history_3y=三年市净率样本|dividend_fiscal_years=各财年实付分红记录|cash_dividend=现金分红总额|parent_profit=归母净利润|dividends=现金分红|buybacks=回购金额|equity_financing_inflow=股权融资流入|latest_dps=最新每股分红|dividend_history=历史分红记录|institution_shares=机构持股数量|institution_total_shares=机构持仓分母股数|total_debt=有息负债|cash=现金|profit=净利润|previous_profit=上期净利润|previous_revenue=上期营业收入|cfo=经营现金流|previous_cfo=上期经营现金流
""".splitlines():
    for pair in line.split("|"):
        if "=" in pair:
            key, value = pair.split("=",1)
            LABELS[key] = value

API_INFO = {
"stock_basic":("stock-basic","证券基础资料","security"),"trade_cal":("trade-calendar","股票交易日历","trade_calendar"),
"daily":("stock-daily","股票日行情","daily_price"),"daily_basic":("stock-daily-basic","每日估值与股本","daily_basic"),
"adj_factor":("stock-adjustment","复权因子","adjustment_factor"),"disclosure_date":("stock-disclosure","财报披露日程","disclosure_schedule"),
"income":("stock-income","利润表","income_statement"),"balancesheet":("stock-balance","资产负债表","balance_sheet"),
"cashflow":("stock-cashflow","现金流量表","cashflow_statement"),"fina_indicator":("stock-financial-indicator","财务指标","financial_indicator"),
"forecast":("stock-forecast","业绩预告","financial_forecast"),"express":("stock-express","业绩快报","financial_express"),
"dividend":("stock-dividend","分红送转记录","dividend"),"repurchase":("stock-repurchase","股份回购","repurchase"),
"share_float":("stock-unlock","限售股解禁","share_unlock"),"stk_holdernumber":("stock-holder-count","股东户数","shareholder_count"),
"top10_holders":("stock-top-holders","前十大股东","top_holder"),"fina_mainbz":("stock-business","主营业务分部","business_segment"),
"index_classify":("stock-classification","行业分类","classification"),"index_member_all":("stock-classification-members","行业成员","classification_member"),
"index_basic":("index-basic","指数基础资料",None),"index_daily":("index-daily","国内指数日行情","market_daily_quote"),
"index_global":("index-global","全球指数日行情","market_daily_quote"),"index_weight":("index-weights","指数成分与权重",None),
"etf_basic":("etf-basic","ETF 基础资料",None),"fund_daily":("etf-daily","ETF 日行情","market_daily_quote"),
"fund_share":("etf-shares","基金份额与规模",None),"fut_basic":("commodity-contracts","期货合约资料",None),
"fut_daily":("commodity-daily","商品期货日行情",None),"fut_mapping":("commodity-mapping","期货主力合约映射",None),
"fut_trade_cal":("commodity-calendar","期货交易日历",None),"fx_daily":("fx-daily","外汇日行情",None),
"bse_mapping":("stock-bse-mapping","北交所证券代码映射",None),"daily_info":("market-daily-summary","市场每日统计",None),
"sz_daily_info":("market-sz-summary","深圳市场每日统计",None),"margin":("market-margin","融资融券余额",None),
}
FORMULA_INFO = {
"realMoneyIndex":("真钱指数","近十二个月经营现金流 ÷ 近十二个月归母净利润","倍"),
"freeCashFlow":("自由现金流","经营现金流 − 资本开支","元"),
"roic":("历史投入资本回报率","税后经营利润 ÷ 平均投入资本","比值"),
"pePercentile":("历史市盈率分位","正值历史样本中不高于当前值的个数 ÷ 正值样本数 × 100","%"),
"pbPercentile":("历史市净率分位","正值历史样本中不高于当前值的个数 ÷ 正值样本数 × 100","%"),
"dividendYears":("连续分红年数","从最近已结束财年向前计数连续存在合格实付现金分红的财年","年"),
"payoutRatio":("派息率","现金分红总额 ÷ 归母净利润","比值"),
"capitalReturnRatio":("股东回报率","（现金分红＋回购－股权融资流入）÷ 归母净利润","比值"),
"expectedDividendYield":("历史预期股息率","最新每股现金分红或历史均值 ÷ 收盘价 × 100","%"),
"institutionHolding":("机构持仓比例","合格机构持股数 ÷ 已核准的总股数","比值"),
"netDebt":("净负债","有息负债 − 现金","元"),
"profitGrowth":("净利润同比增长","（当期净利润 − 上期净利润）÷ |上期净利润|","比值"),
"revenueGrowth":("营业收入同比增长","（当期营业收入 − 上期营业收入）÷ |上期营业收入|","比值"),
"cashflowGrowth":("经营现金流同比增长","（当期经营现金流 − 上期经营现金流）÷ |上期经营现金流|","比值"),
}
GROUP_NAMES = {"usdcnh":"离岸人民币汇率","vix":"美国波动率指数","governmentBonds":"政府债券融资",
"householdCredit":"住户中长期贷款","tier1Housing":"一线城市住宅价格","specialBonds":"地方政府专项债",
"propertyLoans":"房地产贷款","marginBalances":"融资余额","aShareFloatMv":"A 股流通市值","nominalGdp":"名义 GDP 与平减指标"}
for pair in """amort_intang_assets=无形资产摊销|c_disp_withdrwl_invest=收回投资收到现金|c_fr_oth_operate_a=收到其他经营现金|c_inf_fr_operate_a=经营现金流入小计|c_paid_for_taxes=支付各项税费|c_paid_invest=投资支付现金|c_paid_to_for_empl=支付职工现金|c_pay_claims_orig_inco=原保险赔付款|c_recp_return_invest=投资收益收到现金|cip=在建工程|csname=中文简称|debt_to_eqt=负债权益比|defer_tax_assets=递延所得税资产|defer_tax_liab=递延所得税负债|depr_fa_coga_dpba=固定资产等折旧|eff_fx_flu_cash=汇率变动对现金影响|exchange_id=交易所标识|fcff=企业自由现金流|fd_share=基金份额|ifc_cash_incr=收取利息手续费现金净增额|incl_cash_rec_saims=销售商品收到现金|incl_dvd_profit_paid_sc_ms=子公司支付少数股东股利|interestdebt=有息负债|invest_capital=投入资本|invest_real_estate=投资性房地产|is_pub=是否公开|l1_code=一级行业代码|l1_name=一级行业名称|l2_code=二级行业代码|l2_name=二级行业名称|l3_code=三级行业代码|l3_name=三级行业名称|lt_amor_exp=长期待摊费用|lt_amort_deferred_exp=长期待摊费用摊销|n_cap_incr_repur=回购业务资金净增额|n_code=新证券代码|n_depos_incr_fi=客户存款及同业存放净增额|n_disp_subs_oth_biz=处置子公司等收到现金净额|n_inc_borr_oth_fi=向其他金融机构拆入净增额|n_incr_clt_loan_adv=客户贷款及垫款净增额|n_incr_dep_cbob=存放央行及同业净增额|n_incr_disp_faas=处置交易性金融资产净增额|n_incr_disp_tfa=处置金融资产净增额|n_incr_insured_dep=保户储金及投资款净增额|n_incr_loans_cb=向央行借款净增额|n_incr_loans_oth_bank=向其他机构拆入资金净增额|n_incr_pledge_loan=质押贷款净增额|n_recp_disp_fiolta=处置长期资产收回现金净额|n_recp_disp_sobu=处置子公司等收到现金净额|n_reinsur_prem=再保险业务现金净额|net_cash_rece_sec=代理买卖证券收到现金净额|net_dism_capital_add=拆入资金净增加额|netdebt=净债务|o_code=旧证券代码|ocf_to_or=经营现金流营业收入比|oth_cash_pay_oper_act=支付其他经营现金|oth_cash_recp_ral_fnc_act=收到其他筹资现金|oth_cashpay_ral_fnc_act=支付其他筹资现金|oth_pay_ral_inv_act=支付其他投资现金|oth_recp_ral_inv_act=收到其他投资现金|pay_comm_insur_plcy=支付保单红利现金|pay_handling_chrg=支付手续费现金|prem_fr_orig_contr=原保险合同保费现金|proc_issue_bonds=发行债券收到现金|q_eps=单季每股收益|q_roe=单季净资产收益率|recp_tax_rends=收到税费返还|st_cash_out_act=经营现金流出小计|stot_cash_in_fnc_act=筹资现金流入小计|stot_cashout_fnc_act=筹资现金流出小计|stot_inflows_inv_act=投资现金流入小计|stot_out_inv_act=投资现金流出小计|tax_to_ebt=所得税利润总额比|unlock_date=解禁日期""".split("|"):
    key,label=pair.split("=",1)
    LABELS[key]=label
FORMULA_INFO.update({
    "payoutRatio":("派息率","现金分红总额 ÷ 归母净利润 × 100","%"),
    "capitalReturnRatio":("股东回报率","（现金分红＋回购）÷ 股权融资流入 × 100","%"),
    "expectedDividendYield":("历史预期股息率","最新每股分红 ×（1＋三年分红复合增速）÷ 收盘价 × 100；增速限制在正负20%","%"),
    "institutionHolding":("机构持仓比例","合格机构持股数 ÷ 已核准的总股数 × 100","%"),
    "profitGrowth":("净利润同比增长","（当期净利润 − 上期净利润）÷ |上期净利润| × 100","%"),
    "revenueGrowth":("营业收入同比增长","（当期营业收入 − 上期营业收入）÷ |上期营业收入| × 100","%"),
    "cashflowGrowth":("经营现金流同比增长","（当期经营现金流 − 上期经营现金流）÷ |上期经营现金流| × 100","%"),
})
FREQ = {"daily":"日","monthly":"月","quarterly":"季","weekly":"周"}
entries = []
ownership = defaultdict(list)

def judge(value, text, evidence=None):
    return dict(value=value,explanation=text,evidence=evidence or [])

def table(schema,name,label,field=""):
    return dict(schema=schema,table=name,name=label,fieldPath=field)

def field(key,label=None,*,role="source",derived=False,unit="待核验",refs=None,description=None):
    label = label or LABELS.get(key)
    if label and not re.search(r"[\u4e00-\u9fff]",label):
        label={"workbenchPeTtm":"滚动市盈率","workbenchPb":"市净率","ebit":"息税前利润","ebitda":"息税折旧摊销前利润"}.get(key,label+"（财务指标）")
    pending = label is None
    return dict(id=key,name=label or "中文含义待核验",role=role,type="待核验",unit=unit,
                nullable="来源缺失时可为空，具体合同待核验",description=description or ("来源字段的中文语义尚未核准" if pending else label),
                sourceRefs=[],tableRefs=refs or [],derived=judge(derived,"底座计算或转换" if derived else "来源直接提供，供应商自身算法另见来源说明"))

def access(path,paths,method="GET",independent=False):
    return dict(kind="independent" if independent else "aggregate",path=path,method=method,
                responsePaths=paths,parameterNames={},example=None)

def entry(id,name,category,description,fields,tables=None,refs=None,task=None):
    e=dict(id=id,name=name,category=category,description=description,status="已接入，实际覆盖按留存证据核对",
           scope="按现有数据池、日期及采集合同覆盖",sources=[],frequencies=[dict(name="采集规则",description="按所属任务和来源配置执行",task=task)],
           tables=tables or [],history=dict(records=judge(None,"实际历期留存需按来源记录核验"),
            revisions=judge(None,"来源原件与同业务键更新策略须分别核验"),query=judge(False,"当前无此内容的独立历史日期查询合同")),
           derived=judge(False,"直接来源数据；各输出字段的转换另行标明"),fields=fields,calculations=[],access=[],
           evidence=refs or ["docs/tushare-field-consumers.md"])
    entries.append(e)
    return e

def own(identity,e):
    if e["id"] not in ownership[identity]:
        ownership[identity].append(e["id"])

def source_rows():
    rows = read_json(ROOT/"docs/research/investmind-tushare-field-consumers.json")["fields"]
    found = {(r["api"],r["field"]):r for r in rows}
    for api,key in source_contracts(ROOT):
        found.setdefault((api,key),dict(api=api,field=key,coreDatasetTable=None))
    for dataset, fields in FIELD_REGISTRY.items():
        for api in [STANDARD_API_NAMES[dataset],VIP_API_NAMES.get(dataset)]:
            if api:
                for f in fields:
                    found.setdefault((api,f),dict(api=api,field=f,coreDatasetTable=None))
    return list(found.values())

def main():
    ratios=read_json(ROOT/"config/stocks/stock-ratio-catalog.json")["fields"]
    for r in ratios:
        for k in r["keys"]:
            LABELS.setdefault(k,r["label"])
    apis=defaultdict(list)
    for r in source_rows():
        apis[r["api"].removesuffix("_vip")].append(r)
    macro_apis={"cn_gdp","cn_cpi","cn_m","cn_pmi","cn_ppi","repo_daily","us_tycr"}
    for api,rows in sorted(apis.items()):
        if api in macro_apis:
            continue
        id,name,core=API_INFO[api]
        category="stocks" if id.startswith(("stock","trade")) else "markets"
        refs=[table("core",core,name)] if core else []
        refs.append(table("raw","source_payload","供应商原始响应","payload"))
        unique={r["field"]:r for r in rows}
        fs=[]
        for k,r in sorted(unique.items()):
            mapping=[table("core",core,name,k)] if core else [table("raw","source_payload","原始响应", "payload")]
            fs.append(field(k,refs=mapping,description=(LABELS.get(k) or "中文含义待核验")+"；上游 "+api+"."+k))
        task="stocks" if category=="stocks" else "commodities" if id.startswith("commodity") else "market"
        e=entry(id,name,category,"来自 Tushare "+api+" 的"+name+"。标准与批量模式按各自合同、采集记录核对。",fs,refs,task=task)
        e["sources"]=[dict(name="Tushare",api=api,kind="采集渠道",description="原始发布方及实际使用来源以数据主题和已留存原件为准")]
        for r in rows:
            own("tushare:"+r["api"]+"."+r["field"],e)
        if core:
            e["history"]["records"]=judge(True,"保存已经入库的业务日期或报告期记录；实际覆盖日期依留存记录。")
            e["history"]["revisions"]=judge(False,"Core 相同业务键可能更新原值，原件留存与每次修订的完整覆盖另行核验。")
        if category=="stocks":
            e["access"]=[access("/api/v1/stocks/{symbol}",["data（个股工作台，具体字段见下方）"])]
        else:
            e["access"]=[access("/api/v1/market/explorer",["data（市场浏览，具体字段需按来源合同核验）"])]
        if api in {"fut_basic","fut_mapping","fut_trade_cal","index_basic","index_weight","etf_basic","bse_mapping","trade_cal","disclosure_date"}:
            e["access"]=[dict(kind="table",description="已接入底座，按所列原件或核心表查看；未核准为公开接口输出。")]
        if api=="express":
            e["status"]="默认常规停采，历史兼容与显式回填保留"
            e["description"]+=" 新日增量和全池回填默认不规划快报，已有数据及显式重放保留。"
        if api=="share_float":
            e["description"]+=" 当前消费退役与既有历史保存范围见 share-unlock-field-retirement.md。"
            e["evidence"].append("docs/share-unlock-field-retirement.md")
    byid={e["id"]:e for e in entries}
    daily=byid["stock-daily"]
    daily["description"]="保存未复权日行情。现有工作台返回按窗口末日因子归一化的复权四价；接口三公历年窗口与底层历史范围分别计量。"
    daily["tables"].append(table("core","adjustment_factor","复权因子"))
    daily["evidence"].append("docs/workbench-api.md")
    daily["frequencies"]=[dict(name="观测频率",description="每交易日"),dict(name="采集规则",description="证券日任务，默认每天 19:00，Asia/Shanghai；开关和实际生效日程见运行回执",task="stocks")]
    daily["access"]=[access("/api/v1/stocks/{symbol}",["data.dailyChart.candles[]（复权日线）","data.quote（最新报价）"])]
    for key,label in [("date","交易日"),("open","复权开盘价"),("high","复权最高价"),("low","复权最低价"),("close","复权收盘价"),("volume","成交量")]:
        daily["fields"].append(field("data.dailyChart.candles[]."+key,label,role="response",derived=key in {"open","high","low","close"},
                                    refs=[table("core","daily_price","日行情","vol" if key=="volume" else "trade_date" if key=="date" else key)]))
    daily["derived"]=judge(True,"工作台四价发生复权转换；原始入库四价直接来自供应商。")
    daily["calculations"]=[dict(id="adjusted-ohlc",name="复权四价",formula="原始价格 × 当日可用复权因子 ÷ 窗口末日复权因子",
        inputs=[dict(id="raw_price",name="未复权原始价格"),dict(id="adj_factor",name="复权因子")],
        window="三个公历年",version="按当前工作台构建逻辑",missing="缺少可用因子时原逻辑可能用系数 1",
        evidence=["docs/workbench-api.md"])]
    for api in ("income","balancesheet","cashflow","fina_indicator"):
        target=byid[API_INFO[api][0]]
        target["evidence"].extend(["docs/workbench-api.md","docs/research/investmind-workbench-statements.md"])
        target["access"]=[access("/api/v1/stocks/{symbol}",["data.financial（财务报表及指标）"])]
    macro=read_json(ROOT/"config/macro/macro-series.json")["series"]
    for s in macro:
        e=entry(s["id"],s["name"],"macro",s["name"]+"。按观测期和实际公开时点取可见版本，来源按配置优先级选择。",
                [field(s["id"],s["name"],role="response",unit=s["unit"])],
                [table("core","macro_observation","宏观观测"),table("mart","page_snapshot","历史发布快照")],
                ["config/macro/macro-series.json","docs/macro-supply.md","docs/official-source-fields.md"],"macro")
        e["sources"]=[dict(name=src["provider"],api=src.get("api",src.get("seriesId","")),kind="候选来源",
                           description="优先级 "+str(src["priority"])+"；转换 "+src.get("transform","identity")) for src in s["sources"]]
        if not s["sources"]:e["status"]="暂无来源"
        e["frequencies"]=[dict(name="观测频率",description=FREQ.get(s["frequency"],s["frequency"])+"频"),
                          dict(name="采集规则",description="基础宏观周任务，默认每周一 09:00；来源发布节奏与观测频率分别计量",task="macro")]
        e["history"]=dict(records=judge(True,"保存已采集的历期观测及页面版本"),revisions=judge(True,"宏观观测保留版本及来源时点；实际覆盖按观测记录"),
                          query=judge(True,"聚合宏观接口支持 version（发布版本），并非任意日期查询。"))
        e["access"]=[access("/api/v1/macro/insights",["data（宏观洞察中的相应系列）"]),access("/api/v1/macro/transmission",["data（宏观传导系列）"])]
        own("macro:"+s["id"],e)
        for src in s["sources"]:
            if src["provider"]=="tushare":
                for f in src.get("responseFields",[]):
                    own("tushare:"+src["api"]+"."+f,e)
            transform=src.get("transform","identity")
            if transform!="identity":
                formula={"pc1":"（当期值 ÷ 上年同期值 − 1）× 100","chg":"当期值 − 前期值",
                         "qoq_annualized":"［（当季值 ÷ 上季值）^4 − 1］× 100"}.get(transform,transform)
                e["derived"]=judge(True,"该系列存在供应商数值转换，取决于实际使用来源。")
                e["calculations"].append(dict(id=transform,name="来源转换 "+transform,formula=formula,
                    inputs=[dict(id="source_value",name="来源观测值")],window="按观测频率对齐同期",version="macro-series 目录版本",
                    missing="来源缺失或不可比时留空",evidence=["config/macro/macro-series.json"]))
    # 宏观 Tushare 响应中的元数据字段也有可定位的来源说明。
    for api in macro_apis:
        unowned=[r for r in apis.get(api,[]) if not ownership.get("tushare:"+r["api"]+"."+r["field"])]
        if not unowned:continue
        name={"cn_gdp":"国内生产总值原始数据","cn_cpi":"居民消费价格原始数据","cn_m":"货币供应原始数据","cn_pmi":"采购经理指数原始数据","cn_ppi":"工业价格原始数据","repo_daily":"回购利率原始数据","us_tycr":"美国收益率原始数据"}[api]
        e=entry("source-"+api,name,"macro","Tushare "+api+" 的原始合同字段；供宏观系列或扩展专题取数。",
                [field(r["field"]) for r in unowned],[table("raw","source_payload","原始宏观响应")],task="macro")
        e["sources"]=[dict(name="Tushare",api=api,kind="采集渠道",description="具体实际来源按使用系列核验")]
        for r in unowned: own("tushare:"+r["api"]+"."+r["field"],e)
    for gid,g in read_json(ROOT/"config/macro/macro-extensions.json")["groups"].items():
        e=entry("macro-extension-"+gid,GROUP_NAMES[gid],"macro",GROUP_NAMES[gid]+"扩展专题。按专项预算和历史窗口采集，缺口保留为空。",
                [field(gid,GROUP_NAMES[gid],role="response")],[table("raw","source_payload","专题原件"),table("mart","page_snapshot","历史发布快照")],
                ["config/macro/macro-extensions.json","docs/macro-specialties.md","docs/official-source-fields.md"])
        e["sources"]=[dict(name="专题来源",kind="配置来源",description="官方文件、FRED 或 Tushare 的逐组来源见依据文档")]
        schedule=g["schedule"]
        e["frequencies"]=[dict(name="专题采集规则",description=json.dumps(schedule,ensure_ascii=False),
                              config="config/macro/macro-extensions.json#groups."+gid)]
        e["access"]=[access("/api/v1/macro/insights",["data.extensionInsights（专项发布结果）"])]
        e["history"]["records"]=judge(True,"保留已采集的来源原件、已发布专题版本，缺期不补零")
        own("extension:"+gid,e)
    for key,definition in FORMULAS.items():
        label,formula,unit=FORMULA_INFO[key]
        e=entry("metric-"+key,label,"stocks","已登记的股票派生指标。相同英文键的旧版公式与当前工作台指标卡分别说明。",
                [field(key,label,role="response",derived=True,unit=unit)],
                [table("derived","metric_value","派生指标值"),table("derived","metric_input","派生输入引用")],
                ["docs/derived-history.md","backend/app/stocks/derived/registry.py","backend/app/stocks/derived/formulas.py"])
        e["derived"]=judge(True,"由底座公式计算")
        e["calculations"]=[dict(id=key,name=label,formula=formula,inputs=[dict(id=k,name=LABELS.get(k,"输入含义待核验")) for k in definition.inputs],
                               window="具体输入窗口由装载器及公式版本确定，旧样本与当前发布范围分别核对",
                               version=definition.version,missing="缺输入、分母不适用或样本不足时返回空值，详见公式依据",
                               evidence=["backend/app/stocks/derived/formulas.py"])]
        e["history"]=dict(records=judge(True,"保存已计算落库的日期和公式版本，实际覆盖依留存记录"),
                         revisions=judge(False,"同证券、指标、估值日和公式版本重算会更新原结果"),
                         query=judge(False,"当前选股接口读取已发布快照，未提供任意历史日期参数"))
        e["access"]=[access("/api/v1/stocks/screener",["data.items[].researchValues（需按实际字段映射核对）"],"POST")]
        own(f"formula:{key}@{definition.version}",e)
    card_entry=None
    yield_keys={"workbenchFiscalDividendYield":"dividendYield","workbenchAnnualDividendYield":"expectedDividendYield","workbenchNtmDividendYield":"expectedDividendYieldNTM"}
    for key,(label,unit,formula) in DEFINITIONS.items():
        if key in yield_keys:
            if card_entry is None:
                card_entry=entry("stock-dividend-yield","股息率","stocks","三种股息率按财年实派、本年预期及未来十二个月分别计算。",
                    [],[table("derived","metric_value","派生指标值"),table("derived","metric_input","指标输入引用"),
                        table("core","daily_price","日行情"),table("core","daily_basic","每日股本"),
                        table("core","dividend","分红记录"),table("core","income_statement","利润表"),
                        table("core","financial_indicator","财务指标"),table("mart","stock_screener_daily","选股快照")],
                    ["docs/platform-api.md","docs/derived-history.md","backend/app/stocks/derived/engine.py",
                     "backend/app/stocks/derived/workbench_cards.py"],"stocks")
            e=card_entry
        else:
            e=entry("card-"+key,label,"stocks",label+"，工作台指标卡，与同名来源字段或旧公式分别说明。",
                    [],[table("derived","metric_value","派生指标值")],["docs/workbench-api.md","docs/derived-history.md",
                    "backend/app/stocks/derived/workbench_cards.py"],"stocks")
        e["derived"]=judge(True,"底座计算的工作台指标卡")
        e["fields"].append(field(yield_keys.get(key,key),label,role="response",derived=True,unit=unit))
        e["calculations"].append(dict(id=key,name=label,formula=formula,
            inputs=[dict(id="close",name="估值日收盘价"),dict(id="cash_div_tax",name="每股税前现金分红"),
                    dict(id="n_income_attr_p",name="归母净利润"),dict(id="total_share",name="总股本"),
                    dict(id="basic_eps",name="基本每股收益")] if key in yield_keys else
                   [dict(id="metric-card-inputs",name="指标卡装载器提供的价格、股本及已披露财务输入")],
            window="按估值日及当时可见财报；三种股息率分别取上一财年、本年预测、最新披露季度之后四季",
            version=CARD_VERSION,missing="分母非正、输入缺失、修订冲突、分红覆盖或送转基准未证实时为空；完整证据后才能确认零值",
            evidence=["docs/platform-api.md","backend/app/stocks/derived/workbench_cards.py"]))
        e["history"]=dict(records=judge(True,"已计算落库的结果按证券代码、指标标识、估值日及公式版本保存，实际覆盖依记录"),
                         revisions=judge(False,"同一 ts_code（证券代码）、metric_id（指标标识）、as_of（估值日）、formula_version（公式版本）重算更新原记录"),
                         query=judge(False,"当前选股接口不接受任意历史日期参数，读取已发布快照"))
        e["access"]=[access("/api/v1/stocks/screener",["data.items[]（证券行）及 researchValues（研究值）"],"POST"),
                     access("/api/v1/stocks/{symbol}",["data.metrics（工作台指标）"])]
        own(f"card:{key}@{CARD_VERSION}",e)
    for key,(label,formula,threshold) in RULES.items():
        e=entry("risk-"+key,label,"stocks",threshold,[field(key,label,role="response",derived=True)],
                [table("derived","risk_result","当前风险卡")],["docs/workbench-api.md","backend/app/stocks/derived/workbench_risks.py"],"stocks")
        e["derived"]=judge(True,"计算指标并按阈值分级")
        e["history"]=dict(records=judge(False,"带发布版本的风险表保留当前卡片，历史工作台快照未含风险明细"),
                         revisions=judge(False,"相同证券及规则更新当前结果"),query=judge(False,"风险清单仅提供最新结果"))
        e["calculations"]=[dict(id=key,name=label,formula=formula,inputs=[dict(id=key+"-inputs",name="公式所列的同报告期财务与行情输入")],
              window="按规则所列窗口与公开时点取数",version=VERSION,missing="缺输入时状态 unknown（待评估）",evidence=["backend/app/stocks/derived/workbench_risks.py"])]
        e["access"]=[access("/api/v1/stocks/{symbol}",["data.risks.groups[].items[]（风险卡）"])]
        own(f"risk:{key}@{VERSION}",e)
    ratio_entry=entry("stock-ratio-inputs","比率分析输入","stocks","财报字段归一化后供工作台比率分析和用户假设试算。",
             [field(r["id"],r["label"],role="input",description="报表 "+r["table"]+"，原键 "+"/".join(r["keys"])) for r in ratios],
             [table("core","income_statement","利润表"),table("core","balance_sheet","资产负债表"),table("core","cashflow_statement","现金流量表")],
             ["config/stocks/stock-ratio-catalog.json","docs/research/investmind-workbench-panels.md"])
    ratio_entry["access"]=[access("/api/v1/stocks/{symbol}",["data.metrics（财务指标）","data.financial（财报）"])]
    for r in ratios:own("ratio:"+r["id"],ratio_entry)
    extra_content()
    enrich()
    from data_catalog_semantics import audit_semantics
    audit_semantics(entries, ROOT, table, judge, field)
    # 机器回执中同一接口的元字段可能服务多个宏观系列，未命中时不隐式丢失。
    required=identities(ROOT)
    missing=required-set(ownership)
    if missing: raise ValueError("未登记身份 "+str(sorted(missing)))
    from app.main import create_app
    from data_catalog_datasets import register_domain_queries
    spec = create_app().openapi()
    register_domain_queries(entries, spec)
    operations={p:m.upper() for p,v in spec["paths"].items() if p.startswith("/api/v1/") for m in v if m in {"get","post"}}
    for p,m in operations.items():
        for e in entries:
            if p == "/api/v1/data-status" or any(a.get("path")==p and a.get("method")==m for a in e["access"]):
                own(f"operation:{m} {p}",e)
    if any(not ownership.get(f"operation:{m} {p}") for p,m in operations.items()):
        raise ValueError("公共接口未映射")
    out=ROOT/"docs/data-catalog"
    out.mkdir(exist_ok=True)
    files=[]
    groups=[("stocks","股票数据"),("markets","指数、ETF 与商品"),("macro","宏观经济"),("content","财经内容"),("portfolio","公共参考数据")]
    for group,_ in groups:
        filename=group+".json"
        files.append("data-catalog/"+filename)
        (out/filename).write_text(json.dumps([e for e in entries if e["category"]==group],ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    index=dict(version="2026-11-19-v5",categories=[dict(id=id,name=name) for id,name in groups],files=files,ownership=dict(sorted(ownership.items())))
    (ROOT/"docs/data-catalog.json").write_text(json.dumps(index,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"{len(entries)} entries, {len(ownership)} covered identities")

def extra_content():
    content=entry("news","财经快讯","content","新浪财经快讯，按版权状态提供标题、链接以及允许展示的摘要或正文。",
       [field(k,n,role="response") for k,n in [("id","记录编号"),("title","标题"),("source","来源"),("sourceUrl","原文链接"),
        ("publishedAt","发布时间"),("summary","摘要"),("body","正文"),("copyrightStatus","版权状态"),("nextCursor","下一页游标")]],
       [table("ops","news_delivery","采集交付队列"),table("core","content_document","公开内容"),table("ops","job_cursor","采集游标")],
       ["docs/news-api.md","backend/app/content/routes.py"],"news")
    content["sources"]=[dict(name="新浪财经",kind="原始发布与采集渠道",description="按新闻配置分页采集")]
    content["history"]=dict(records=judge(True,"保存新闻记录，默认保留365天；具体保留配置及实际清理记录另查"),
                           revisions=judge(False,"相同来源身份可更新；未承诺每次修改前正文留存"),
                           query=judge(True,"新闻接口可按游标翻阅已留存记录，尚无任意起止日期参数"))
    content["access"]=[access("/api/v1/news",["items[]（快讯）","health（采集状态）"],independent=True)]
    for id,name,desc in [("external-macro","宏观文件导入","按外部文件合同导入宏观观测"),
                         ("external-content","公开内容文件导入","按外部文件合同导入公开内容")]:
        e=entry(id,name,"content",desc,[field("source","来源标识"),field("value" if id.endswith("macro") else "title","观测数值" if id.endswith("macro") else "标题")],
                [table("raw","external_file","导入文件"),table("core","macro_observation" if id.endswith("macro") else "content_document",name)],
                ["backend/app/connectors/external_files/contracts.py","docs/official-source-fields.md"])
        e["access"]=[dict(kind="table",description="通过现有 CLI 导入，查询已入库记录；导入不由文档页触发。")]
    for id,name,category,path,desc in [
        ("market-overview","市场统计与情绪","markets","/api/v1/market/overview","按已发布证券池汇总市场成交、涨跌分布与情绪。"),
        ("market-explorer","市场浏览与热力图","markets","/api/v1/market/explorer","汇集行业、地域、ETF、商品与新闻的已发布产品。"),
        ("stock-research","证券配对研究","stocks","/api/v1/stocks/research","按同源、同版、同日的已发布证券输入进行配对比较。"),
        ("security-search","证券搜索","stocks","/api/v1/search","按证券代码与名称读取已发布的搜索候选。"),
        ("macro-enterprise","企业范围与官方目标","macro","/api/v1/macro/transmission","将企业财报与行业及官方目标按同版范围对照。"),
        ("macro-spreads","宏观利差与剪刀差","macro","/api/v1/macro/insights","按两条基础系列的真实共同日期生成差值。"),
        ("portfolio-inputs","组合公共参考与分红","portfolio","/api/v1/portfolio","提供组合所需的公共参考行情、基准和分红输入；用户持仓及试算参数由消费项目管理。")]:
        ref="docs/portfolio-split-lineage.md" if category=="portfolio" else "docs/macro-supply.md" if category=="macro" else "docs/market-supply.md" if category=="markets" else "docs/security-search.md" if id=="security-search" else "docs/workbench-api.md"
        e=entry(id,name,category,desc,[field(id,name,role="response",derived=id in {"market-overview","market-explorer","macro-spreads","stock-research"})],
               [table("mart","published_snapshot","当前发布指针"),table("mart","page_snapshot","发布历史")],[ref])
        e["access"]=[access(path,["data（"+name+"发布对象）"],"POST" if id=="stock-research" else "GET",independent=id=="security-search")]
        if id=="macro-spreads":
            e["derived"]=judge(True,"按共同可见日期对齐基础系列后计算差值")
            e["calculations"]=[dict(id="paired-spread",name="同日差值",formula="左侧序列数值 − 右侧序列数值",
                inputs=[dict(id="left",name="左侧同日观测"),dict(id="right",name="右侧同日观测")],window="两侧真实共同日期",
                version="随宏观目录和发布版本",missing="缺任意一侧时留空",evidence=["docs/macro-supply.md"])]
        if id=="stock-research":e["history"]["records"]=judge(False,"研究响应按已发布输入计算，无独立研究结果历史")
    # 地域不具备历史映射回填能力。
    e=entry("security-region","证券地域","stocks","刷新证券资料的地区映射；当前资料与历史时点映射分别计量。",
       [field("area","地区",role="storage",refs=[table("core","security","证券资料","data.area")])],[table("core","security","证券资料","data.area")],["docs/market-supply.md"],"regions")
    e["access"]=[access("/api/v1/market/explorer",["data（地域聚合）"])]
    e["history"]["records"]=judge(False,"证券资料保存当前地域映射，历史逐日映射需额外来源")

def enrich():
    from app.db.base import ALL_BASES
    from app.db import models
    definitions={t.fullname:t for base in ALL_BASES for t in base.metadata.tables.values()}
    payload_fields={
      "market-overview":[("marketSnapshot","行情概览"),("turnoverSnapshot","成交统计"),("breadthSnapshots","涨跌宽度"),
                         ("sentimentSnapshot","情绪指标"),("globalIndices","全球指数"),("etfQuotes","ETF 报价")],
      "market-explorer":[("heatmapData","热力图分组"),("regionMetadata","地域覆盖说明"),("rankingLeaders","涨幅排行"),
                        ("rankingDecliners","跌幅排行"),("commodityData","商品行情"),("marketNews","市场快讯")],
      "stock-research":[("current","当前证券"),("peer","对比证券"),("instruments","可选证券"),("scope","比较范围"),("metrics","研究指标"),("pair","配对结果")],
      "security-search":[("query","归一化搜索词"),("stocks.items[]","证券结果"),("stocks.total","匹配数量"),("stocks.page","页码"),("stocks.pageSize","每页条数"),("stocks.status","查询状态")],
      "portfolio-inputs":[("assets","可选资产"),("prices","复权价格"),("unadjustedPrices","未复权价格"),("factorCoverage","复权覆盖"),
                          ("dividendLedger","分红账本"),("dividendYieldInputs","股息率输入"),("attributionInputs","收益归因输入"),
                          ("benchmarks","全收益基准"),("treasuryReference","国债参考")],
    }
    for e in entries:
        if e["id"] in payload_fields:
            e["fields"]=[field("data."+key,label,role="response") for key,label in payload_fields[e["id"]]]
            e["access"][0]["responsePaths"]=[f["id"]+"（"+f["name"]+"）" for f in e["fields"]]
    # 上游字段与 Core 普通列、JSON 子键的实际写入路径分别核对。
    for e in entries:
        for f in e["fields"]:
            mapped=[]
            for ref in f["tableRefs"]:
                target=definitions.get(ref["schema"]+"."+ref["table"])
                key=ref["fieldPath"]
                if target is not None and key and key.split(".")[0] not in target.c:
                    alias={"market":"board","list_status":"status"}.get(key) if ref["table"]=="security" else None
                    if alias:
                        ref["fieldPath"]=alias
                    elif ref["table"]=="business_segment" and key.startswith("bz_"):
                        ref=table("raw","source_payload","主营专题原件","payload")
                        f["description"]+="；专题 bz_* 字段保存在来源原件，不能假定已投影为通用 Core 列。"
                    elif "data" in target.c:
                        ref["fieldPath"]="data."+key
                    else:
                        ref=table("raw","source_payload","来源原件","payload")
                mapped.append(ref)
            f["tableRefs"]=mapped
    from app.macro.derivations import SPREADS
    from app.connectors.external_files.contracts import MacroImportRecord, ContentImportRecord
    imported_labels=dict(schemaVersion="合同版本",recordType="记录类型",source="来源",seriesId="宏观系列标识",
        name="名称",region="地区",frequency="观测频率",unit="单位",seasonalAdjustment="季节调整方式",observedAt="观测日期",
        releasedAt="公开时刻",revision="修订版本",value="观测数值",sourceUrl="原文链接",externalId="外部编号",
        contentType="内容类型",title="标题",summary="摘要",body="正文",publishedAt="发布时间",securities="证券代码列表",
        securityNames="证券名称列表",copyrightStatus="版权状态",ingestionRevision="导入修订版本")
    for name,model in (("macro",MacroImportRecord),("content",ContentImportRecord)):
        target=next(e for e in entries if e["id"]=="external-"+name)
        target["fields"]=[]
        schema=model.model_json_schema(by_alias=True)
        for key,s in schema["properties"].items():
            f=field(key,imported_labels[key],role="input")
            f["type"]=s.get("type","可空联合类型")
            f["nullable"]="必填" if key in schema.get("required",[]) else "可省略"
            target["fields"].append(f)
            own("import:"+name+"."+key,target)
    official=next(e for e in entries if e["id"]=="macro-enterprise")
    official["evidence"].append("config/macro/enterprise-targets.json")
    official["fields"]=[]
    for target in read_json(ROOT/"config/macro/enterprise-targets.json")["targets"]:
        official["fields"].append(field(target["code"],target["label"],role="response",description="官方目标，类型 "+target["kind"]+"；与数据池交集及实际成分证据分别核验"))
        own("target:"+target["code"],official)
    historical=read_json(ROOT/"docs/research/investmind-non-tushare-field-usage.json")
    for e in entries:
        witness=historical["basicSeries"].get(e["id"])
        if witness:
            e["evidence"].append("docs/research/investmind-non-tushare-field-usage.json")
            e["sources"].append(dict(name=witness.get("publishedSource") or "未供给",kind="历史实供来源",
                description="核验时刻 "+historical["checkedAt"]+"；发布版本 "+historical["version"]+"；当前实供须核对运行记录"))
            for a in e["access"]:
                kind="macro-insights" if a["path"].endswith("insights") else "macro-transmission"
                paths=witness.get("publishedFieldPaths",{}).get(kind,[])
                a["responsePaths"]=["data."+p+"（按系列标识 "+e["id"]+" 定位；核验版本 "+historical["version"]+"）" for p in paths]
    labels={s["id"]:s["name"] for s in read_json(ROOT/"config/macro/macro-series.json")["series"]}
    for id,label,left,right,freq,unit,scale in SPREADS:
        e=entry(id,label,"macro","按两侧真实共同日期计算的宏观差值，缺期不向前填充。",
                [field(id,label,role="response",derived=True,unit=unit)],
                [table("core","macro_observation","宏观输入观测"),table("mart","page_snapshot","历史发布结果")],
                ["backend/app/macro/derivations.py","docs/macro-supply.md"])
        e["derived"]=judge(True,"两条基础系列对齐后相减")
        e["calculations"]=[dict(id=id,name=label,formula=f"（{labels[left]} − {labels[right]}）× {scale}",
            inputs=[dict(id=left,name=labels[left]),dict(id=right,name=labels[right])],
            window="仅使用两侧共同日期，"+FREQ[freq]+"频",version="当前 SPREADS 定义",
            missing="任意一侧缺值、最新日期不一致或输入滞后时显示原因",
            evidence=["backend/app/macro/derivations.py"])]
        e["history"]=dict(records=judge(True,"历史派生点随页面快照保存，基础观测各自保留"),
                         revisions=judge(True,"保留已发布页面版本；同版本内部重算记录不单列"),
                         query=judge(True,"宏观聚合接口支持 version（发布版本）查询"))
        e["access"]=[access("/api/v1/macro/insights",["data（按宏观派生项 id 定位）"])]
        own("spread:"+id,e)
    for e in entries:
        for f in e["fields"]:
            for ref in f["tableRefs"]:
                target=definitions.get(ref["schema"]+"."+ref["table"])
                column=target.c.get(ref["fieldPath"]) if target is not None else None
                if column is not None:
                    f["type"]="存储类型 "+str(column.type)
                    f["nullable"]="允许为空" if column.nullable else "存储列非空"
            if f["type"]=="待核验":
                f["type"]="来源合同字段（类型待核验）" if f["role"]=="source" else "数值或对象，依具体响应合同"
            if e["id"]=="stock-daily":
                key=f["id"].split(".")[-1]
                f["unit"]={"open":"元","close":"元","high":"元","low":"元","pre_close":"元","change":"元",
                           "pct_chg":"%","vol":"手","volume":"手","amount":"千元"}.get(key,"不适用")
        if e["category"]=="stocks" and e["derived"]["value"] is True and not e["sources"]:
            e["sources"]=[dict(name="Tushare 及已核验财务输入",kind="计算输入",description="输入来源与取版规则见对应原始数据条目及公式依据")]
    yields=next(e for e in entries if e["id"]=="stock-dividend-yield")
    for c in yields["calculations"]:
        if c["id"]=="workbenchFiscalDividendYield":
            c["window"]="快照上一财年；只计估值日之前已支付的合格正现金事件"
            c["inputs"]=[dict(id="cash_div_tax",name="归属财年已派税前每股分红"),dict(id="close",name="估值日收盘价")]
        else:
            c["formula"]+="；加权派息率 =（较早财年派息率 × 3＋较近财年派息率 × 5）÷ 8，财年派息率为每股分红 ÷ 正年度基本每股收益"
            c["inputs"]=[dict(id="n_income_attr_p",name="已披露归母净利润"),dict(id="total_share",name="同日总股本"),
                         dict(id="cash_div_tax",name="历史税前每股分红"),dict(id="basic_eps",name="正年度基本每股收益"),
                         dict(id="close",name="估值日收盘价")]
            c["window"]="本年采用最新中期利润与前五年同期占比合格样本；未来十二个月采用最新已披露季度之后四季"
            c["formula"]+=("；本年利润预测 = 最新可用中期累计归母净利 ÷ 前五年同期与全年利润占比的合格样本均值"
                if c["id"]=="workbenchAnnualDividendYield" else "；累计报表先转单季，再按上年同季 × 2 − 前年同季预测")
    # 这些数据只更新当前状态，历期原件和当前归一化表分别计量。
    for e in entries:
        if e["id"] in {"stock-basic","stock-bse-mapping"}:
            e["history"]["records"]=judge(False,"证券资料更新当前值；逐日历史资料须另查原件与发布快照")
        if e["id"].startswith("metric-"):
            e["access"]=[dict(kind="table",description="本条为历史登记公式，当前发布可能采用其他窗口或计算路径。查看已留存指标，现行口径另见工作台卡片和选股字段说明。")]
            e["status"]="历史公式留存；现行显示口径须按消费字段核对"
    current_screener()

def current_screener():
    from app.stocks.rea_fields import REA_THRESHOLD_UI
    announcements=entry("stock-announcements","证券公告","stocks","巨潮公告按证券、日期窗口与页码采集；上游适配范围和已查窗口决定覆盖，空值不等于已查无公告。",
        [field(k,n,role="response") for k,n in [("id","公告编号"),("title","公告标题"),("sourceUrl","原文链接"),
            ("publishedAt","披露日期"),("securities","关联证券"),("placementStatus","日线标点定位状态"),("announcementCoverage","已查公告窗口")]],
        [table("raw","source_payload","公告原始页"),table("core","content_document","公告文档"),table("ops","job_cursor","公告窗口游标")],
        ["docs/workbench-announcements.md","backend/app/content/announcement_history.py"],"announcements")
    announcements["sources"]=[dict(name="巨潮资讯",api="information/topSearch/query · hisAnnouncement/query",kind="采集渠道",
                                   description="先唯一识别证券，再按日期窗分页，公开列表的披露时刻与日期区别说明")]
    announcements["history"]=dict(records=judge(True,"保存已采集公告及来源页，实际完整范围由已完成的窗口证据决定"),
        revisions=judge(None,"公告文档身份与原件保留分开核验，完整逐次修订覆盖待核验"),
        query=judge(False,"工作台返回与已发布日线范围关联的公告，没有独立任意日期范围的公告接口"))
    announcements["access"]=[access("/api/v1/stocks/{symbol}",["data.dailyChart.announcements[]（公告标点）","data.dailyChart.announcementCoverage.windows[]（已查窗口）"])]
    own("source:cninfo-announcements",announcements)
    fiscal={
      "realMoneyIndex":("经营活动现金净额合计 ÷ 归母净利润合计",["n_cashflow_act","n_income_attr_p"]),
      "ocfToRevenue":("经营活动现金净额合计 ÷ 营业收入合计",["n_cashflow_act","revenue"]),
      "ocfToTotalLiab":("经营活动现金净额合计 ÷ 各期末负债总额的合计",["n_cashflow_act","total_liab"]),
      "capexCoverageRatio":("经营活动现金净额合计 ÷ 购建长期资产付现合计",["n_cashflow_act","c_pay_acq_const_fiolta"]),
      "cashCollectionRatio":("销售商品及劳务收现合计 ÷ 营业收入合计",["c_fr_sale_sg","revenue"]),
      "cashPaymentRatio":("购货付现合计 ÷ 营业成本合计",["c_paid_goods_s","oper_cost"]),
      "freeCashFlowFy":("（逐年经营现金流 − 资本开支）求和 ÷ 一亿",["n_cashflow_act","c_pay_acq_const_fiolta"]),
      "freeCashFlowMarginFy":("同期自由现金流合计 ÷ 营业收入合计",["n_cashflow_act","c_pay_acq_const_fiolta","revenue"]),
    }
    LABELS.update(c_fr_sale_sg="销售商品提供劳务收到现金",c_paid_goods_s="购买商品接受劳务支付现金")
    grouped={}
    byid={e["id"]:e for e in entries}
    for row in REA_THRESHOLD_UI:
        key=row["key"]
        if key in {"dividendYield","expectedDividendYield","expectedDividendYieldNTM"}:
            own("screener:"+key,byid["stock-dividend-yield"]);continue
        base=re.sub(r"Cum(?:3|5|10)y$","",key)
        if base not in grouped:
            e=entry("screener-"+base,row["label"],"stocks","当前选股发布字段；年度累计口径与旧注册公式分别说明。",
                [],[table("mart","stock_screener_daily","选股发布行"),table("mart","published_snapshot","选股发布指针")],
                ["docs/screener-api-fields.md","docs/screener-fiscal-cash.md","backend/app/stocks/research.py",
                 "backend/app/stocks/marts/screener_list.py"],"stocks")
            e["access"]=[access("/api/v1/stocks/screener",["data.items[].researchValues."+key+"（"+row["label"]+"）"],"POST")]
            e["history"]=dict(records=judge(True,"保存已发布的选股快照历史；并非每个计算过程都另行保存"),
                             revisions=judge(True,"保留不同发布版本；同一版本内部的每次重算不单列"),
                             query=judge(False,"当前选股请求使用已发布快照，未提供历史日期参数"))
            grouped[base]=e
        e=grouped[base]
        formula=fiscal.get(base)
        e["fields"].append(field(key,row["label"],role="response",derived=formula is not None,unit=row["unit"] or "倍或计数，依字段定义"))
        if key!=base:e["access"][0]["responsePaths"].append("data.items[].researchValues."+key+"（"+row["label"]+"）")
        if formula:
            width=re.search(r"Cum(3|5|10)y$",key)
            e["derived"]=judge(True,"当前完整财年或多财年累计公式")
            e["calculations"].append(dict(id=key,name=row["label"],formula=formula[0],
                inputs=[dict(id=k,name=LABELS[k]) for k in formula[1]],window=(width[1]+"个" if width else "最近一个")+"完整财年",
                version="当前选股发布口径，annual-OCF/parent-profit 等以字段依据为准",
                missing="按估值日可见年报取数；每年先核全再累计；同版冲突、缺科目或分母为零时留空；自由现金流要求资本开支非负",
                evidence=["docs/screener-fiscal-cash.md","backend/app/stocks/research.py"]))
        elif base=="capitalReturnRatio":
            e["derived"]=judge(True,"分红与回购相对股权筹资的财年累计比值")
            e["calculations"].append(dict(id=key,name=row["label"],formula="（已核实现金分红＋完成回购金额）÷ 上市公司股权筹资流入",
              inputs=[dict(id="dividends",name="已核实现金分红"),dict(id="buybacks",name="完成回购金额"),dict(id="financing",name="上市公司股权筹资分项")],
              window="字段指定的完整财年或多财年",version="当前 list_projection",
              missing="股权筹资零值、仅有合并吸收投资总额、缺分红或回购见证时均为空；不得以旧公式结果代替",evidence=["backend/app/stocks/marts/screener_list.py"]))
        else:
            direct={"pe":("core.daily_basic.pe","静态市盈率"),"pb":("core.daily_basic.pb","市净率"),
                    "roe":("core.financial_indicator.data.roe","最新已披露报告的净资产收益率"),
                    "roic":("core.financial_indicator.data.roic","年报披露的投入资本回报率")}
            extra={
              "pePercentile":("五年正值市盈率样本中不高于当前值的个数 ÷ 正值样本总数 × 100","五年完整开市日窗口"),
              "pbPercentile":("五年正值市净率样本中不高于当前值的个数 ÷ 正值样本总数 × 100","五年完整开市日窗口"),
              "dividendYears":("从最近完整财年逐年向前计数连续合格正现金实派的财年","逐年核全，遇明确零分红停止；最多回溯至上市年"),
              "payoutRatio":("最近三完整财年各自的（已派现金金额 ÷ 同年可见归母净利润 × 100）的算术平均","最近三个完整财年"),
              "institutionHolding":("最近可见报告期内，前十大中按名称去重的可识别机构持股占比之和","最近已披露报告期"),
              "institutionHoldingChange":("本期可识别机构占比 − 上期可识别机构占比，单位百分点","最近两个均有效的报告期"),
              "marketCap":("来源总市值（万元）÷ 10000，输出亿元","估值业务日"),
            }
            e["evidence"].append("docs/screener-capital-fields.md")
            if base in direct:
                path,meaning=direct[base]
                e["derived"]=judge(False,"来源直接提供，底座按公开时点选取，"+meaning)
                e["fields"][-1]["description"]=meaning+"；"+path
                e["fields"][-1]["derived"]=e["derived"].copy()
            elif base in extra:
                expression,window=extra[base]
                e["derived"]=judge(True,"按已披露数据和核准窗口计算")
                e["fields"][-1]["derived"]=e["derived"].copy()
                e["calculations"].append(dict(id=key,name=row["label"],formula=expression,
                    inputs=[dict(id=base+"-source",name="公式所列的行情、分红、财报或机构持仓输入")],
                    window=window,version="当前选股发布口径",
                    missing="窗口不完整、版本冲突、分母无效或来源覆盖未证实时为空，具体见逐字段核验依据",
                    evidence=["docs/screener-capital-fields.md","docs/screener-api-fields.md"]))
            else:
                e["derived"]=judge(False,"当前未产出可用的计算值，缺分类、公募去重或筹码变化投影证据，缺项保持为空")
                e["fields"][-1]["derived"]=e["derived"].copy()
                e["status"]="已登记字段，当前投影缺值，完整计算能力待补充"
        own("screener:"+key,e)

if __name__=="__main__":
    main()
