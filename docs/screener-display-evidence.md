# 选股展示字段的报表与风险核对

业务日 2026-10-21，选股发布版本 `20261021-1c01ff6eb0b7d78a`，50 只证券。391 个年报期、400 个单季报告期合计 9,492 个趋势值。8,972 个数值与原料重算一致，520 个双方均空。另核过十年估值 100 个空值和 250 条风险状态。逐股、逐期重算值、已选报表原件页及行号、Core 行号、估值窗覆盖和风险输入均可从证券代码查到。[逐证券证据](research/investmind-screener-display.json)

## 年度与单季财务趋势

从已核验合同的 Tushare 财报原件读取整期行，并独立读取 `core.income_statement`、`core.cashflow_statement`、`core.balance_sheet`。只采纳报告期、公告日均不晚于业务日的季度末合并报表。取版优先类型 1 或 4，最后使用调整前报告 5；同级按有效公告日、调整类型、修订标记、原公告日取最新。同版优先整期原件，现金流核心表允许补同版原件未请求的科目；同一科目有冲突则空。仅复用原件验真输入，未调用发布器的 `financial_research` 或使用已发布工作台表格作计算输入。

金额从元除以一亿。财年取年末报告，单季第一季保留累计值，其他季用本期累计减同年上一季累计。资产负债存量取期末余额。年度、单季各取最近八个可见报告，上市时间短可少于八期。重算同时核对发布报告期序列、字段集合及值。数值采用相对容差 10⁻⁹、绝对容差 10⁻⁸。

| 展示字段 | Tushare 原列及计算 | 年报有值 / 391 | 单季有值 / 400 |
| --- | --- | ---: | ---: |
| revenue | income.revenue，缺时取 total_revenue | 391 | 400 |
| profit | income.n_income_attr_p，归母净利润 | 391 | 400 |
| cashflow | cashflow.n_cashflow_act | 391 | 400 |
| investmentCash | cashflow.n_cashflow_inv_act | 391 | 400 |
| financingCash | cashflow.n_cash_flows_fnc_act | 391 | 400 |
| contractLiability | balancesheet.contract_liab | 301 | 379 |
| receivable | balancesheet.accounts_receiv | 337 | 352 |
| inventory | balancesheet.inventories | 333 | 345 |
| payable | balancesheet.acct_payable | 342 | 352 |
| grossMargin | （营业收入 − income.oper_cost）÷ 营业收入 × 100 | 343 | 352 |
| netMargin | income.n_income ÷ 营业收入 × 100，分子为合并净利润 | 391 | 400 |
| revenueBasis | 年报取全年营收；季报取截至该季的连续四季收入之和 | 391 | 399 |

上述四个余额列的“占营收比例”使用相同报告期的 `revenueBasis`。报告缺科目、分母为零或缺少连续四季时留空。北交所证券 `920002.BJ` 有一期余额可展示但缺满四季的营收分母。

## 十年 PE 与 PB 分位

分别重读证券上市日期及交易所、Core 日历与通过合同验真的上交所日历、停牌记录和量额，再与截至业务日的估值序列核对。深交所可参照上交所日历，已有的深交所记录优先。完整十年内的期望交易日扣除已核实的全天停牌；窗口完整、当日倍数为正且历史无缺时，才计算正值样本中不高于当日的比例。

两项各有 13 只证券因上市期限或上市日期条件无有效值，其余 37 只因尚缺连续十年日历留空。100 个空值都有已发布解释。该时点可检验空值门槛，尚无可用于检验非空十年分位的完整真实窗口。五年分位使用单独窗口。[五年分位证据](screener-capital-fields.md)

## 风险列

每只证券按固定展示顺序核对五个 `key/status`，逐条检查 `period/detail` 存在。财务修订输入取项目正式风险原料读取器，净现金读取工作台指标卡的输入报表。以下状态由独立算式得出。报告期及参与判定的披露值见证据文件的 `riskInputs`；本次未逐字比较提示文案。

| 风险键 | 核验口径 | 当前状态分布 |
| --- | --- | --- |
| highCashHighDebt | money_cap / total_assets >25% 且同期有息负债 / total_assets >35%。有息负债需短贷、长贷、短债、应付债券、租赁负债、到期非流动负债，可与 interestdebt 勾稽后取值。 | 未知 50 |
| goodwill | goodwill / total_assets 严格超过 20% 提醒，超过 35% 危险。 | 正常 31，提醒 1，未知 18 |
| dividendVsFinancing | 最近三财年逐年复核实付分红、经营现金及借款、发债、吸收投资三类流入；分红覆盖不足且有融资为危险，三项融资确定为零时提醒，关键输入缺失时未知。 | 正常 38，危险 9，未知 3 |
| realMoney | 最近完整财年经营现金净额 / 归母净利 <0.8 提醒。 | 正常 37，提醒 13 |
| netCash | 非金融合并报表货币资金减可核实有息债务，结果为负则提醒，金融报表或无法勾稽时未知。 | 未知 50 |

## 全部注册显示列的核验状态

下表覆盖 46 个注册列 ID。普通卡片及悬浮明细在同一证券行上投影，卡片实际能展示哪些列还取决于分组与前四列顺序。字段使用位置见 InvestMind 网站仓库的 `docs/screener-field-usage.md`。

| 显示列 | 已核验内容与边界 |
| --- | --- |
| name、price、marketCap | 名称、代码、收盘价、涨跌和从万元转亿元的市值已对当日 Core。交易所标签从代码后缀生成。[当日对账](research/investmind-screener-field-contracts.json) |
| pe、pb | 当日倍数、五年分位、十年窗口缺数均有证据；当前无非空十年样本。 |
| industry、board、concept、region | 使用大类/细分行业、地域、板块、概念标签。板块逐证券核过筛选成员，分类筛选核过样例。industry 明细读取子行业与地域，board 也用于卡片；后三者无独立普通表格列。供应商标签分类的沿革不在本次财报重算范围。 |
| sector、thsIndustry、bankCentralBorrowRatio、bankInterbankLiabilityRatio | 已检查配置，页面无独立显示列。 |
| risk | 五项状态及缺数分布见上表。 |
| dividendYield、payoutRatio、dividendYears | 财年及两种预期股息率有指标卡证据；财年分红年数和三年支付率从分红记录复核。支付率浮层另外列最新报告参考，其口径独立于三年均值。[股息试点](research/investmind-dividend-lineage-observed.json) · [股东回报核验](screener-capital-fields.md) |
| shareholderReturn | FY、3/5/10 年资本回报对股权筹资之比缺上市公司筹资分项及回购依据，200 个研究层空值已核。[回购和融资边界](screener-capital-fields.md) |
| roeRoic | 年报 ROIC 与核心年报百分数逐只对账；ROE 阈值按最新报告已核。该列按所选报告窗读取逐期 ROE/ROIC；阈值或年报对账不能证明所有季度明细均已逐期重算。[ROIC 证据](research/investmind-screener-field-contracts.json) |
| revenueTrend、profit、cashflowTrend、marginTrend、contractLiabilityTrend、receivableTrend、inventoryTrend、payableTrend | 每只证券的年度/单季来源金额、利润率、余额及收入分母逐格核对。均值、同比和期间范围由界面在相同序列上计算。 |
| revenueGrowth、profitGrowth | 银行表中增速使用年度与单季金额计算同比、环比和累计同比；上述金额已逐期复核，页面另有根同比参考值，其公式须与财报期间分别追溯。 |
| realMoneyIndex、cashQualityRatios、cashOcfRevenue、cashOcfLiab、cashCapexCoverage、cashFreeCashFlow、cashFcfMargin、cashCollection、cashPayment | 八套 FY 与 3/5/10 年累计口径已有 1,600 值重算，风险项另核单财年真钱阈值。[现金指标核验](screener-fiscal-cash.md) |
| institutionHolding、longMoneyFund、chipConcentration | 机构本期与较上期变化已核，长钱及基金缺正式覆盖。筹码显示按户数历史比较，研究层变化字段为空；前十机构核验不证明所有历史明细。[持有人与缺源](screener-capital-fields.md) |
| ahPriceGap、bankExpectedYield、bankNimTrend、bankAssetQuality、bankScore | 银行表及卡片依赖同一 bankEvaluation。现有 3 家银行，缺同日 A/H 配对汇率、预测、息差、拨备等正式输入，默认五项总分均空，已有覆盖与缺项详情可核。[银行字段与请求](screener-api-fields.md) |

使用有本机数据库只读权限的项目 Python 运行 `python -I scripts/audit-screener-display.py --output docs/research/investmind-screener-display.json`。脚本对期间、证券、字段、数值、风险顺序及状态执行断言，发生数值差异时记录在 `differences` 并返回非零状态。
