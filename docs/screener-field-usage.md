# InvestMind 选股器字段用途

本页回答某个字段在选股器的哪些地方使用。可直接搜索接口字段 ID，查出它参与的显示列、筛选预设、排序模板、图轴和银行条件。字段路径以选股响应的 `items[]` 为根。接口至数据底座的溯源另见[公共字段消费索引](field-consumers.md)。

## 如何读字段表

- `R` 表示 `researchValues`，`T` 表示 `financialTrends.annual[]` 或 `financialTrends.quarterly[]`，`B` 表示 `bankEvaluation`。未加前缀的是证券行根字段。
- `W.key` 表示 `researchValues.key` 以及同前缀的 `Cum3y`、`Cum5y`、`Cum10y` 四个已发布窗口。FY 日期读最近一期年度财务趋势；缺值解释按同一字段键取 `listMissingReasons`。普通表格及悬浮明细共用同一次 `resultMetric` 投影。
- 普通卡片固定显示名称、代码、价格、涨跌、行业与板块；其余指标按当前列顺序取前四个，只显示投影主值。表中的普通卡片列列出各固定分组和可选自定义场景。普通表格的 `custom` 表示自定义默认固定列，`all` 中其他列也可单独勾选。
- 只有启用的阈值才筛选。服务端逐行先读 `researchValues[key]`，缺键才尝试同名根值；显式空值和非有限数均不匹配，PE/PB 非正也不匹配。普通排序复用这个字段优先级。各普通滑条方向由配置及预设决定，小盘价值对市值有独立方向。高级排序可选升降序，快速排序点击表头切换方向。普通条件有效数读 `bootstrap.availableCounts[key]`，普通总池读 `bootstrap.totalCount`。
- 普通表格 PE/PB 与散点图共用五年分位取值逻辑。优先读取研究字段，显式空值保留为空；仅缺键且对应 `metricBasis` 标为 `five-year-complete-window-required` 才接受根分位。研究字段的零分位为有效数。散点只收录双轴均为有限值的点，PE 的范围为 (0, 100]，PB 为 (0, 20]。行业、标签、刷选和前沿叠加依赖 `majorIndustry/name/symbol` 及选中轴的方向；初始轴为 `expectedDividendYieldNTM` 与 `realMoneyIndexCum5y`。
- 银行列表还检查 `majorIndustry` 为银行且有 `bankEvaluation`。银行表的通用列沿用年度投影。银行卡片另有固定 PB、报告 ROE、总分、启用项数和 A/H 折溢价；明细从同一报告读取。条件支持 >、≥、<、≤，启用后缺值不匹配。覆盖数读 `bank.availableCounts[field]/bank.universeCount`。排序按代码或总分降序（缺分排末）；评分权重、N 和条件经 `request.bank` 交由服务端计算。评分明细读取五项 `B.scores/weighted`，九个输入为 `B.inputs.{revisedYield,revisedGrowth,pb,offensivePbScore,largeBankScore,nim,yearEndNim,provision,yearEndProvision}`，溯源或缺数解释读取 `B.sources/reasons`。

## 重复读取的核验边界


用途由 InvestMind 的字段注册、排序、图轴和模板生成。此表随消费项目版本同步；底座运行不依赖网站源码。

<!-- 自动生成的字段表 -->

## 显示列、卡片与明细

| 列 ID | 名称 | 普通表格分组 | 普通卡片 | 银行表格 | 实际读取的投影 |
| --- | --- | --- | --- | --- | --- |
| name | 名称 | 固定 | 固定 | 固定 | name、symbol（名称、代码、交易所、工作台链接） |
| industry | 行业 | default、all、custom、quality、growth、dividend、holding | 固定 | — | majorIndustry；明细 subIndustry、region |
| board | 板块 | — | 固定 | — | 列已隐藏；board 在板块筛选和卡片标签使用 |
| sector | 赛道 | — | — | — | 无独立显示列 |
| thsIndustry | 细分行业 | — | — | — | 无独立显示列 |
| concept | 概念 | — | — | — | 列已隐藏；concepts 在分类筛选中使用 |
| region | 地域 | — | — | — | 列已隐藏；region 在分类筛选及行业明细中使用 |
| risk | 风险 | default、all、custom、quality、growth、dividend、holding | default、all、custom、quality、growth、dividend、holding | 有 | listRisks[].{key,status,period,detail}；旧快照按 R.realMoneyIndex / realMoneyIndex 推断真钱项 |
| price | 现价 | default、all、custom、quality、growth、dividend、holding | 固定 | 有 | price、change |
| ahPriceGap | A/H价差 | — | — | 有 | B.ahDiscount |
| pe | PE | default、all、growth | default、all、growth、custom 可选 | 有 | pe；R.pePercentile、R.pePercentile10y；五年分位见正文 |
| pb | PB | default、all、growth | default、all、growth、custom 可选 | 有 | pb；R.pbPercentile、R.pbPercentile10y；五年分位见正文 |
| dividendYield | 股息率(FY) | default、all、dividend | default、all、dividend、custom 可选 | 有 | R.{dividendYield,expectedDividendYield,expectedDividendYieldNTM}、metricBasis.dividendYield |
| bankExpectedYield | 预期股息 | — | — | 有 | B.fields.{bankRevisedExpectedDividendYield,bankRevisedExpectedGrowth} |
| payoutRatio | 股利支付率 | default、all、dividend | dividend、custom 可选 | — | R.payoutRatio；listHistory.dividends[].{year,cashAmount,observedCashAmount,parentProfit,payoutRatio}；最新财年取有年份的单期支付率；旧序列 payoutRatios |
| dividendYears | 连续分红 | all、dividend | dividend、custom 可选 | — | dividendYears、listMissingReasons.dividendYears |
| roeRoic | ROE/ROIC | default、all、quality、growth | quality、growth、custom 可选 | — | listHistory.returns[].{period,frequency,roe,roic}；所选期间无序列时只回退 roe；季度明细另列年报 ROIC |
| revenueGrowth | 营收增 | — | — | 有 | T.{period,revenue}，全年/累计/单季同比与环比；参考 revenueGrowth |
| profitGrowth | 利润增 | — | — | 有 | T.{period,profit}，全年/累计/单季同比与环比；参考 profitGrowth |
| revenueTrend | 营收 | all、growth | custom 可选 | — | T.{period,revenue} |
| profit | 利润 | default、all、quality、growth、dividend、holding | quality、holding、custom 可选 | — | T.{period,profit}，旧年度回退 profitTrend |
| cashflowTrend | 经现 | all、quality、dividend | quality、custom 可选 | — | T.{period,cashflow,investmentCash,financingCash} |
| marginTrend | 毛利 | all、quality、growth | custom 可选 | — | T.{period,grossMargin,netMargin}，根 grossMargin 为旧参考 |
| contractLiabilityTrend | 合负 | all、quality | custom 可选 | — | T.{period,contractLiability,revenueBasis}；年末余额/全年收入或季末余额/近四季收入 |
| receivableTrend | 应收 | all、quality | custom 可选 | — | T.{period,receivable,revenueBasis}；年末余额/全年收入或季末余额/近四季收入 |
| inventoryTrend | 存货 | all、quality | custom 可选 | — | T.{period,inventory,revenueBasis}；年末余额/全年收入或季末余额/近四季收入 |
| payableTrend | 应付 | all、quality | custom 可选 | — | T.{period,payable,revenueBasis}；年末余额/全年收入或季末余额/近四季收入 |
| institutionHolding | 机构 | all、holding | holding、custom 可选 | — | institutionHolding、R.institutionHoldingChange、listHistory.institutions[].{period,value} |
| longMoneyFund | 长线持仓/基金数 | all、holding | holding、custom 可选 | — | R.{longMoneyHoldingRatio,fundHeavyCount} |
| chipConcentration | 户数 | all、holding | custom 可选 | — | listHistory.holders[].{period,value}；回退 R.{chipConcentrationChange,shareholderCount} |
| realMoneyIndex | 真钱 | default、all、quality | custom 可选 | 有 | W.realMoneyIndex；当期 R 缺键才回退 realMoneyIndex |
| cashQualityRatios | 现金质量 | all、quality | custom 可选 | — | R.{ocfToRevenue,ocfToTotalLiab,capexCoverageRatio,freeCashFlowFy,freeCashFlowMarginFy,cashCollectionRatio,cashPaymentRatio} |
| cashOcfRevenue | 经现/营收 | all | custom 可选 | — | W.ocfToRevenue |
| cashOcfLiab | 经现/负债 | all | custom 可选 | — | W.ocfToTotalLiab |
| cashCapexCoverage | 开支保障 | all | custom 可选 | — | W.capexCoverageRatio |
| cashFreeCashFlow | 自由CF | all | custom 可选 | — | W.freeCashFlowFy |
| cashFcfMargin | FCF率 | all | custom 可选 | — | W.freeCashFlowMarginFy |
| cashCollection | 收现比 | all | custom 可选 | — | W.cashCollectionRatio |
| cashPayment | 付现比 | all | custom 可选 | — | W.cashPaymentRatio |
| bankNimTrend | 息差走势 | — | — | 有 | B.fields.netInterestMargin |
| bankAssetQuality | 不良/拨备 | — | — | 有 | B.fields.{nplRatio,provisionCoverage,coreTier1CapitalAdequacyRatio,loanDepositRatio} |
| bankScore | 经验分 | — | — | 有 | B.{total,enabledCount,scores,weighted,sources,reasons,inputs,fields,algorithmVersion,asOf,ahDiscount} |
| bankCentralBorrowRatio | 央借依赖 | — | — | — | 无独立显示列或同名银行条件 |
| bankInterbankLiabilityRatio | 同业结构 | — | — | — | 无独立显示列或同名银行条件 |
| marketCap | 市值 | default、all、quality、growth、dividend、holding | custom 可选 | — | marketCap |
| shareholderReturn | 回报/融资 | default、all、dividend | custom 可选 | — | W.capitalReturnRatio |

## 普通阈值、排序与散点轴反查

| 字段 | 页面名称 | 阈值组 / 初设 | 滑条范围 / 步长 | 排序 | 轴读值 / 偏好 | 关联显示列（具体口径见上表） |
| --- | --- | --- | --- | --- | --- | --- |
| pe | PE · 市盈率 | valuation <15 | 0–100 / 1 | 高级、表头 | pe / 低优 | pe |
| pb | PB · 市净率 | valuation <2 | 0–20 / 0.1 | 高级、表头 | pb / 低优 | pb |
| pePercentile | PE分位 · 5年 | valuation <30% | 0–100 / 1 | 高级 | R.pePercentile / 已核实五年根值 / 低优 | pe |
| pbPercentile | PB分位 · 5年 | valuation <30% | 0–100 / 1 | 高级 | R.pbPercentile / 已核实五年根值 / 低优 | pb |
| dividendYield | 股息率(FY·财年归属) · 按 fiscal_year 归属 | dividend >4% | 0–10 / 0.1 | 高级、表头 | R.dividendYield / 高优 | dividendYield |
| expectedDividendYield | 预期股息率(本年度·季节性权重) | dividend >4% | 0–10 / 0.1 | 高级 | R.expectedDividendYield / 高优 | dividendYield |
| expectedDividendYieldNTM | 预期股息率(未来12月·NTM) | dividend >4% | 0–10 / 0.1 | 高级 | R.expectedDividendYieldNTM / 高优 | dividendYield |
| dividendYears | 连续分红 · 年数 | dividend ≥5年 | 0–30 / 1 | 高级 | dividendYears / 高优 | dividendYears |
| payoutRatio | 股利支付率 · 3年均值 | dividend >30% | 0–50 / 1 | 高级 | R.payoutRatio / 高优 | payoutRatio |
| capitalReturnRatio | 资本回报/股权筹资(FY) · 分红+回购/股权筹资 | dividend >1 | 0–3 / 0.1 | 高级 | R.capitalReturnRatio / 高优 | shareholderReturn |
| capitalReturnRatioCum3y | 资本回报/股权筹资(3年) · 累计口径 | dividend >1 | 0–3 / 0.1 | 高级 | R.capitalReturnRatioCum3y / 高优 | shareholderReturn |
| capitalReturnRatioCum5y | 资本回报/股权筹资(5年) · 累计口径 | dividend >1 | 0–3 / 0.1 | 高级 | R.capitalReturnRatioCum5y / 高优 | shareholderReturn |
| capitalReturnRatioCum10y | 资本回报/股权筹资(10年) · 累计口径 | dividend >1 | 0–3 / 0.1 | 高级 | R.capitalReturnRatioCum10y / 高优 | shareholderReturn |
| roe | ROE · 净资产收益率 | quality >10% | 0–50 / 1 | 高级 | R.roe / 缺键回退根值 / 高优 | roeRoic |
| roic | 年报 ROIC · 投入资本回报率 | quality >10% | 0–50 / 1 | 高级 | listHistory.returns[].roic 最近八个 FY 内最新有效值 / 高优 | roeRoic |
| realMoneyIndex | 真钱指数 · 现金流/归母净利润 | quality >1 | 0–3 / 0.1 | 高级、表头 | R.realMoneyIndex / 高优 | risk、realMoneyIndex |
| realMoneyIndexCum3y | 真钱指数(3年) · 年报累计口径 | quality >1 | 0–3 / 0.1 | 高级 | R.realMoneyIndexCum3y / 高优 | realMoneyIndex |
| realMoneyIndexCum5y | 真钱指数(5年) · 年报累计口径 | quality >1 | 0–3 / 0.1 | 高级 | R.realMoneyIndexCum5y / 高优 | realMoneyIndex |
| realMoneyIndexCum10y | 真钱指数(10年) · 年报累计口径 | quality >1 | 0–3 / 0.1 | 高级 | R.realMoneyIndexCum10y / 高优 | realMoneyIndex |
| ocfToRevenue | 经现/营收 · FY年报 | quality >0.1 | 0–3 / 0.1 | 高级 | — | cashQualityRatios、cashOcfRevenue |
| ocfToRevenueCum3y | 经现/营收(3年) · 累计口径 | quality >0.1 | 0–3 / 0.1 | 高级 | — | cashOcfRevenue |
| ocfToRevenueCum5y | 经现/营收(5年) · 累计口径 | quality >0.1 | 0–3 / 0.1 | 高级 | — | cashOcfRevenue |
| ocfToRevenueCum10y | 经现/营收(10年) · 累计口径 | quality >0.1 | 0–3 / 0.1 | 高级 | — | cashOcfRevenue |
| ocfToTotalLiab | 经现/负债 · FY年报 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashQualityRatios、cashOcfLiab |
| ocfToTotalLiabCum3y | 经现/负债(3年) · 累计口径 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashOcfLiab |
| ocfToTotalLiabCum5y | 经现/负债(5年) · 累计口径 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashOcfLiab |
| ocfToTotalLiabCum10y | 经现/负债(10年) · 累计口径 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashOcfLiab |
| capexCoverageRatio | 资本开支保障 · A股 OCF/capex | quality >1 | 0–3 / 0.1 | 高级 | — | cashQualityRatios、cashCapexCoverage |
| capexCoverageRatioCum3y | 资本开支保障(3年) · 累计口径 | quality >1 | 0–3 / 0.1 | 高级 | — | cashCapexCoverage |
| capexCoverageRatioCum5y | 资本开支保障(5年) · 累计口径 | quality >1 | 0–3 / 0.1 | 高级 | — | cashCapexCoverage |
| capexCoverageRatioCum10y | 资本开支保障(10年) · 累计口径 | quality >1 | 0–3 / 0.1 | 高级 | — | cashCapexCoverage |
| freeCashFlowFy | 自由现金流 · A股 OCF-capex | quality >0亿 | 10–10000 / 1（对数滑条） | 高级 | — | cashQualityRatios、cashFreeCashFlow |
| freeCashFlowFyCum3y | 自由现金流(3年) · 累计口径 | quality >0亿 | 10–10000 / 1（对数滑条） | 高级 | — | cashFreeCashFlow |
| freeCashFlowFyCum5y | 自由现金流(5年) · 累计口径 | quality >0亿 | 10–10000 / 1（对数滑条） | 高级 | — | cashFreeCashFlow |
| freeCashFlowFyCum10y | 自由现金流(10年) · 累计口径 | quality >0亿 | 10–10000 / 1（对数滑条） | 高级 | — | cashFreeCashFlow |
| freeCashFlowMarginFy | 自由现金流率 · A股 OCF-capex/营收 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashQualityRatios、cashFcfMargin |
| freeCashFlowMarginFyCum3y | 自由现金流率(3年) · 累计口径 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashFcfMargin |
| freeCashFlowMarginFyCum5y | 自由现金流率(5年) · 累计口径 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashFcfMargin |
| freeCashFlowMarginFyCum10y | 自由现金流率(10年) · 累计口径 | quality >0.05 | 0–3 / 0.1 | 高级 | — | cashFcfMargin |
| cashCollectionRatio | 收现比 · A股现金流明细 | quality >1 | 0–3 / 0.1 | 高级 | — | cashQualityRatios、cashCollection |
| cashCollectionRatioCum3y | 收现比(3年) · 累计口径 | quality >1 | 0–3 / 0.1 | 高级 | — | cashCollection |
| cashCollectionRatioCum5y | 收现比(5年) · 累计口径 | quality >1 | 0–3 / 0.1 | 高级 | — | cashCollection |
| cashCollectionRatioCum10y | 收现比(10年) · 累计口径 | quality >1 | 0–3 / 0.1 | 高级 | — | cashCollection |
| cashPaymentRatio | 付现比 · A股现金流明细 | quality <1 | 0–3 / 0.1 | 高级 | — | cashQualityRatios、cashPayment |
| cashPaymentRatioCum3y | 付现比(3年) · 累计口径 | quality <1 | 0–3 / 0.1 | 高级 | — | cashPayment |
| cashPaymentRatioCum5y | 付现比(5年) · 累计口径 | quality <1 | 0–3 / 0.1 | 高级 | — | cashPayment |
| cashPaymentRatioCum10y | 付现比(10年) · 累计口径 | quality <1 | 0–3 / 0.1 | 高级 | — | cashPayment |
| institutionHolding | 机构持仓 · 前十大流通股东合计 | institution >5% | 0–50 / 1 | 高级 | — | institutionHolding |
| institutionHoldingChange | 机构增减 · 较上期变化 | institution >0% | 0–10 / 0.1 | 高级 | — | institutionHolding |
| longMoneyHoldingRatio | 长钱持仓 · 国家队/社保/险资/QFII | institution >1% | 0–50 / 1 | 高级 | — | longMoneyFund |
| fundHeavyCount | 基金重仓数 · 公募重仓 | institution >5次 | 0–10 / 1 | 高级 | — | longMoneyFund |
| chipConcentrationChange | 筹码集中 · 股东户数下降 | institution >0% | 0–10 / 0.1 | 高级 | — | chipConcentration |
| marketCap | 市值 · 亿元 | size >100亿 | 10–10000 / 1（对数滑条） | 高级、表头 | marketCap / 高优 | marketCap |
| profitGrowth | 净利润同比 | — | — | 高级 | — | profitGrowth |
| revenueGrowingFy | FY营收增长（布尔） | — | — | 高级 | — | — |
| ocfGrowingFy | FY经营现金流增长（布尔） | — | — | 高级 | — | — |

## 普通筛选预设

| 预设 ID | 名称 | 启用的条件（含实际方向） |
| --- | --- | --- |
| high-dividend-stable | 高股息稳健 | dividendYield > 4，dividendYears ≥ 5，pe < 15 |
| cash-cow | 现金奶牛 | realMoneyIndex > 1.2，roe > 10，dividendYield > 3 |
| low-valuation-bluechip | 低估蓝筹 | pePercentile < 30，pbPercentile < 30，marketCap > 500 |
| quality-growth | 优质成长 | roe > 15，roic > 12，realMoneyIndex > 1 |
| deep-value | 深度价值 | pe < 10，pb < 1.5，dividendYield > 3 |
| dividend-aristocrat | 分红贵族 | dividendYears ≥ 10，dividendYield > 3，roe > 8 |
| small-cap-value | 小盘价值 | marketCap < 200，pe < 15，pb < 2，dividendYield > 2 |
| high-roe-low-pe | 高ROE低PE | roe > 20，pe < 20，realMoneyIndex > 0.8 |
| ntm-dividend-hunter | NTM股息猎手 | expectedDividendYieldNTM > 5，pe < 20，marketCap > 100 |
| historical-low | 历史低位 | pePercentile < 20，pbPercentile < 20 |

## 排序模板

| 模板 | 字段、方向与权重 |
| --- | --- |
| 默认 | dividendYield desc ×100，roe desc ×100（未启用），pe asc ×100（未启用） |
| 高股息 | dividendYield desc ×70，expectedDividendYieldNTM desc ×30 |
| 分红稳健 | dividendYield desc ×40，dividendYears desc ×35，payoutRatio desc ×25 |
| 质量优先 | roe desc ×40，roic desc ×30，realMoneyIndex desc ×30 |
| 便宜好公司 | pePercentile asc ×40，pbPercentile asc ×30，roe desc ×30 |
| 真钱＋分红 | realMoneyIndexCum5y desc ×40，dividendYield desc ×30，expectedDividendYieldNTM desc ×30 |
| 增长偏好 | revenueGrowingFy desc ×60，ocfGrowingFy desc ×40 |
| 大盘稳健 | marketCap desc ×40，dividendYield desc ×30，roe desc ×30 |

## 分类、板块和排除

| 控制项 | 字段 ID | 用途或取值 |
| --- | --- | --- |
| 大类行业 | majorIndustry | majorIndustry |
| 细分行业 | subIndustry | subIndustry |
| 概念 | concept | concepts[] |
| 地域 | region | region |
| 板块 | board | sh-main 沪市主板，sz-main 深市主板，star 科创板，chinext 创业板，bse 北交所，hk 港股（通资格待核验） |
| 亏损股 | loss | isLoss、pe < 0 |
| ST/*ST | st | isST、name 含 ST |
| 存贷双高 | inventoryRisk | inventoryRisk |
| 真钱不足 | lowRealMoney | realMoneyIndex < 0.8 |
| 净负债 | netDebt | netDebt |
| 业绩下滑 | performanceDecline | profitTrend 非空项连续下降且至少两项 |
| 不分红 | noDividend | payoutRatios 恰有三项且均为 0 |
| 主营下滑 | revenueDecline | revenueGrowth ≤ 0 |
| 现金下滑 | cashflowDecline | cashflowGrowth ≤ 0 |

## 银行独立条件和显示用途

| 银行条件字段 | 名称 | 初设 | 投影 | 卡片 |
| --- | --- | --- | --- | --- |
| pb | PB | <1.2倍 | B.fields.pb | 摘要、明细 |
| pbPercentile10y | PB分位（10年） | <40% | B.fields.pbPercentile10y | 明细 |
| dividendYield | FY股息率 | >4% | B.fields.dividendYield | 明细 |
| expectedDividendYield | 本年度预期股息率 | >4% | B.fields.expectedDividendYield | 明细 |
| bankRevisedExpectedDividendYield | 修正预期股息率 | >5% | B.fields.bankRevisedExpectedDividendYield | 摘要、明细 |
| netInterestMargin | 净息差 | >1.8% | B.fields.netInterestMargin | 摘要、明细 |
| bankRevisedExpectedGrowth | 修正预期增速 | >3% | B.fields.bankRevisedExpectedGrowth | 摘要、明细 |
| roe | 报告ROE（非TTM核验） | >10% | B.fields.roe | 摘要、明细 |
| nplRatio | 不良率 | <1.5% | B.fields.nplRatio | 摘要、明细 |
| provisionCoverage | 拨备覆盖率 | >250% | B.fields.provisionCoverage | 摘要、明细 |
| loanDepositRatio | 贷存比 | <90% | B.fields.loanDepositRatio | 摘要、明细 |
| coreTier1CapitalAdequacyRatio | 核心一级资本充足率 | >8% | B.fields.coreTier1CapitalAdequacyRatio | 摘要、明细 |
| bankDefensiveScore | 来源表防守分 | >6分 | B.fields.bankDefensiveScore | 明细 |
| bankOffensiveScore | 来源表进攻分 | >6分 | B.fields.bankOffensiveScore | 明细 |
| dilutionEventCount3y | 近3年稀释次数 | ≤0次 | B.fields.dilutionEventCount3y | 明细 |

## 银行评分预设

| 预设 | 名称 | 增长折算 N | yield | pb | large | nim | provision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| defensive | 防守型 | 5 | 100 | 100 | 100 | 100 | 100 |
| balanced | 攻防均衡 | 4 | 100 | 100 | 100 | 100 | 100 |
| offensive | 进攻型 | 3 | 100 | 100 | 100 | 100 | 100 |
| quality | 质量稳健 | 5 | 80 | 80 | 80 | 140 | 140 |
| dividend | 股息估值 | 5 | 130 | 130 | 100 | 80 | 80 |
