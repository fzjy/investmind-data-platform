# 选股器接口字段与实际供给

2026-11-14 核对已发布选股池，业务日为 2026-10-21，版本为 `20261021-1c01ff6eb0b7d78a`。全池 50 只，含 3 只银行。以下覆盖数分母均为这 50 只，银行单列。 [逐字段观测值、缺失证券及请求结果](research/investmind-screener-field-contracts.json)

## 如何读一次选股响应

InvestMind 的普通筛选、散点和银行模式均消费 `POST /api/v1/stocks/screener`。请求包括 `filters`、`sortRules`、`sortMode`、分页及可选 `bank`。服务读取已发布的 `screener` 快照并过滤、排序、分页。响应 `data.snapshot` 指认版本，`items` 给出证券行，`total` 是筛选后的总数，`bootstrap` 给出条件定义和可用计数。发布时的逐证券版本行存于 `mart.stock_screener_daily`，产品指针在 `mart.published_snapshot`。日行情随交易日变化，报表随公告和修订变化，组合条件与银行排序在请求时计算。实际任务运行记录见 [股息率试点观测](research/investmind-dividend-lineage-observed.json)。

普通数值筛选先按键查 `items[].researchValues`，键不存在才读取行顶层。显式空值会排除该证券。PE、PB 另外要求有限且严格为正。43 个行顶层字段的存在数和非空数见证据的 `displayFieldCoverage`，容器非空只表示容器存在。每个可筛选字段的实际单位、方向、默认门槛、满足数量、缺失证券和其中有专属缺项说明的数量见 `thresholds`。

## 字段与底层输入

表中的核验分两层。所有 53 个门槛均从真实接口的逐证券研究值独立计算成员，再与启用单一门槛的 API 结果及全池可用计数对账。原值对账另列在“核验深度”栏。阅读公式可以定位生产路径；逐期原件及原始单位须有单独的重算证据才能验收。

| 字段 | 供应商输入、表与公式 | 核验深度 |
|---|---|---|
| `symbol,name,price,change,pe,pb,marketCap` | Tushare `stock_basic` → `core.security.ts_code/name`，`daily` → `core.daily_price.close/pct_chg`，`daily_basic` → `core.daily_basic.pe/pb/total_mv`。市值从万元除以 10,000 转亿元，涨跌幅为百分数。发布器按截至业务日最近记录取值。 | 本日 50 只联结全部匹配，六个值字段 294 个非空相等，PE 6 个两端均空。此结果适用于本业务日。 |
| `pePercentile,pbPercentile` | 核验原件估值窗与后续 `core.daily_basic` 衔接；五年完整交易日历与交易日集合相符、当日为正、无缺失后，计算正值观察中小于等于当日的比例 × 100。列表另有 `researchValues.pePercentile10y/pbPercentile10y`，十年窗另查日历、上市日期和全天停牌。 | 五年口径分别有 31、36 只可用，缺值的 19、14 只未带该字段专属说明。十年结果不得填入五年条件。已按 1,212 个开市日独立核对完整窗口并重算五年分位。见 [估值与股东回报核验](screener-capital-fields.md)。 |
| `dividendYield,expectedDividendYield,expectedDividendYieldNTM` | Tushare `dividend/daily` 等分红事件、财年覆盖、股本及业务日股价 → 财年已派现金、本年季节性预测及未来十二月预测三张工作台指标卡，单位均为 %。 | 49、42、39 只有值，三种值及 20 个空值已在试点按证券与有版本的指标卡对过。 [试点证据](research/investmind-dividend-lineage-observed.json) |
| `dividendYears,payoutRatio` | 从上一财年向前数有正数已派现金的连续完整财年；支付率为最近三个完整财年各自实付总金额 ÷ 同财年归母净利润 × 100 后取均值，亏损或缺完整记录返回空。依赖 `core.dividend`、现金派发覆盖及 `core.income_statement.n_income_attr_p`。 | 分别 43、40 只可用，7、10 个缺值都有缺项说明。已用有覆盖的逐年派息记录重算连续年数，按事件的实付金额和同期归母净利复核三年均值。 |
| `capitalReturnRatio` 及其三种累计窗 | 目标为（已付分红＋已完成回购）÷ 上市公司股权筹资流入。对应 `core.dividend`、`core.repurchase` 与 `core.cashflow_statement.c_recp_cap_contrib`。只有合并融资总项、缺回购完成金额或分母为零时均无法建立有效比率。 | 四窗各为 0/50，各有 50 个专属说明。顶层旧口径仍有 18 个非空值；四个筛选条件及研究散点须尊重研究层的显式 null。 |
| `roe,roic,grossMargin,netMargin` | Tushare `fina_indicator/fina_indicator_vip` → `core.financial_indicator`。ROE、毛利率、净利率取最新可见报告。ROIC 按最近八份可见 FY 报告中最新的有限 `roic`，单位 %，由构建及读旧快照时统一投影至顶层和研究字段。 | ROE 50，ROIC 44，毛利率 44，净利率 50。44 个 ROIC 与可见 FY 的核心百分数逐只相等，范围 −48.0011～40.5978，20 只满足 >10%。另 6 只最近八份年报缺披露，空值均有字段原因。原发布快照的 47 个旧比值保留在历史版本；详见 JSON 的 `roicUnits`。 |
| `realMoneyIndex,ocfToRevenue,ocfToTotalLiab,capexCoverageRatio,cashCollectionRatio,cashPaymentRatio` 及各自三种累计窗 | Tushare `cashflow` → `core.cashflow_statement` 之 `n_cashflow_act,c_pay_acq_const_fiolta,c_fr_sale_sg,c_paid_goods_s`；`income` → `core.income_statement` 之 `n_income_attr_p,revenue/total_revenue,oper_cost`；`balancesheet` → `core.balance_sheet.total_liab`。六个比值依次为经营现金流/归母净利、经营现金流/营收、经营现金流/负债、经营现金流/资本开支、销售收现/营收、购货付现/营业成本。 | 按连续财年先分子分母分别加总再相除。三种累计后缀为 `Cum3y,Cum5y,Cum10y`。缺年份、缺有效科目或分母为零则为空。每窗覆盖数和有原因的空值见 JSON；已独立选择报告版本并执行 1,600 个单元的重算。见 [逐股财报重算](screener-fiscal-cash.md)。 |
| `freeCashFlowFy,freeCashFlowMarginFy` 及其三种累计窗 | 同上现金流量表及利润表。自由现金流为各年经营现金流减资本开支之和，以亿元计；自由现金流率为该数除以同期营收之和，按比值使用。要求资本开支非负。 | 两组 FY、3、5、10 年分别为 50、50、48、45。已与原件重算结果及筛选成员分别对账。 |
| `institutionHolding,institutionHoldingChange` | Tushare `top10_holders` → `core.top_holder` 中可识别的前十大持有人，取来源总股本比例，或按同期持股量和总股本推算。最新期加总已分类机构持股占总股本的百分比；相邻两个可见报告期之差单位为百分点。 | 两者均为 43 只可用，7 个空值均缺字段专属说明。范围只覆盖前十已识别机构，按正式持有人分类、最近两期核心披露和所需股本分母重算，与 50 只公开结果相符。 |
| `longMoneyHoldingRatio,fundHeavyCount,chipConcentrationChange` | 长钱比例需同报告期的机构分类和总股本；基金重仓数需完整公募披露和基金标识；筹码集中变化需同口径的前后股东户数。股东户数历史另由 Tushare `stk_holdernumber` → `core.shareholder_count.holder_num`、公告日、报告期供给。 | 三项均为 0/50。前两项有专属原因 50 条，筹码集中变化无专属原因。存在户数历史不证明该变化字段已计算。 |
| `revenueGrowth,profitGrowth,cashflowGrowth,revenueGrowingFy,ocfGrowingFy,profitTrend` | 同比采用截至业务日最新可比报告期，以（本期－上年同期）/上年同期绝对值 × 100 计算。`revenueGrowingFy,ocfGrowingFy` 为相邻完整财年是否增长的 0/1。`profitTrend` 取最近三年归母净利。原字段为营业收入、净利润、经营活动现金净流量；同比输入有利润回退顺序，见 `derived/inputs.py`。 | 同比三项各 50 只有值；快速排序额外支持 `profitGrowth,revenueGrowingFy,ocfGrowingFy`。未独立复算所有可比期选择。 |

### 展示字段、分类和风险

- `industry,majorIndustry,subIndustry,region` 来自证券资料与分类映射，`concepts` 来自证券资料中的概念数组。`board,isST,listingYears` 来自证券档案、上市日和交易所映射。本快照板块依次为沪主板 21、深主板 13、科创板 5、创业板 8、北交所 3、港股 0。核过行业、细行业与地区各一个有值分类的证券成员。50 只的概念数组都没有可用于正向筛选的成员。
- `financialTrends.annual/quarterly` 各最多八个期间，金额为亿元。年报取 12 月期末，季度流量由同年累计值相减，资产负债表取期末余额。字段为 `period,revenue,profit,cashflow,investmentCash,financingCash,contractLiability,receivable,inventory,payable,grossMargin,netMargin,revenueBasis`。除上表外，原字段为 `n_cashflow_inv_act,n_cash_flows_fnc_act,contract_liab,accounts_receiv,inventories,acct_payable,n_income`。季度 `revenueBasis` 需连续四季收入。按公告日选当时可见版本。
- `listHistory.holders/institutions/returns/dividends` 分别保留已披露户数、可识别机构占比、财报 ROE/ROIC、三财年支付率及其完整性证据。`metricBasis` 保存字段口径，`listMissingReasons` 保存发布时记录的逐字段原因。原始历史和派生研究值各有其期间和单位。
- `incomeSafety` 来自已有 `realMoneyIndex` 派生记录，`isLoss` 判利润负值，`inventoryRisk` 判存货/TTM 营收 ≥ 0.4，`netDebt` 判净债务/权益 ≥ 1。`listRisks` 固定输出高存高贷、商誉、分红与融资、真钱指数和净现金五项，包括 `key,status,period,detail`。缺输入可为 `unknown`；排除项有独立谓词，不能按这五项标签推断。

## 请求选项与银行评分

十个预设逐只核过启用的门槛交集。快速排序逐项核过 56 个字段的降序，缺值放末位、相同值按证券代码排序。七个高级模板独立用样本标准差标准化后按权重、方向求和，再乘有效指标数占启用数的比例，所有证券的顺序一致。预设和模板具体键、阈值、权重及结果指纹均写入 JSON。九个排除项和六个板块均按证券集合复核，五页合计 50 个不重复代码，所得成员摘要和分页结果见证据，定义见 `backend/app/stocks/service.py`。普通散点 20 条候选轴复用上述研究字段和行情值，同一证券任一轴缺值便无该点；图表还过滤 PE > 100 或 PB > 20 的对应轴；轴配对与图表交互由网站侧核验。

银行模式限制 `majorIndustry=银行`。本业务日为平安银行、招商银行、工商银行。五个配置分别为 `defensive,balanced,offensive,quality,dividend`，版本 `rea-bank-score-20261020`。配置及展示字段来自 `config/financial-indicators.json`，逐证券输出 `bankEvaluation.inputs/fields/sources/scores/weighted/total/reasons`。15 个银行字段各用一次 ≥0 条件核对证券成员；五个配置都核了全池、可用字段数及总分是否可用。

| 银行字段 | 实际供给与口径 |
|---|---|
| `pb,roe,dividendYield,expectedDividendYield` | 三家均有值，依次取正 PB、最新报告 ROE、研究层财年及本年预期股息率。报告 ROE 缺银行专属评分口径认证。 |
| `pbPercentile10y,bankRevisedExpectedDividendYield,bankRevisedExpectedGrowth,netInterestMargin,nplRatio,provisionCoverage,loanDepositRatio,coreTier1CapitalAdequacyRatio,dilutionEventCount3y` | 三家均空。当前银行投影仅发布上行四项；十年分位不会自动接入普通研究层，缺正式可见日、来源或核准输入合同。 |
| `bankDefensiveScore,bankOffensiveScore` | 三家均空。评分分项为修正预期股息率 + 修正增长/除数、PB 倒数（特定配置可给核准进攻分）、银行规模分、本期与上年末净息差方向、拨备覆盖率变化/10（上限 1）。启用项须齐全才能出加权总分。三家在五个模板下均缺总分。A/H 折价另需两腿价格、汇率、证券映射及同日基准，当前亦空。 |

## 重放与核验边界

从带后端依赖和只读数据库连接的底座根目录运行 `python -I scripts/audit-screener-field-contracts.py`。脚本使用生产路由、数据库 provider 和专用只读事务；多组合请求在进程内完成，不触发采集或发布。实际分红历史可见范围另见试点证据。这里记录的只是该版本全量 50 只的接口供给；跨快照、历史原件重选、全部嵌套金额逐期重算和供应商侧真实更新频率仍需独立证据。资本回报双口径、集中度变化与银行源缺口应在切换前分别跟踪。
