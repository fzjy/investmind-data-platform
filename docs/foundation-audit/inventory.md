# 全域数据与消费者清单

本次核对五域目录中 141 个条目，按实际注册路由核对归属。表中的公共路径省略共同前缀 `/api/v1/datasets/`。输入计数只证明留存凭证；计数为零的条目继续区分配置、上游缺口、字段缺失和产品无值。

每项的来源、字段、任务、表、业务键、时间列、历史口径、API、直接和间接消费者、实库样本或查询位于机器清单。每个字段的单位、可空性、来源列和落表位置通过该项的 `fieldDetails` JSON Pointer 定位。仅有代码引用或对外路由，不足以证明某个页面实际调用。未登记的消费位置不推测。日期列以交易所业务日、报告期或事件日解释，数据库的带时区时间列以 UTC 记录。本机调度时区为 Asia/Shanghai。宏观观测日与发布时间、内容发布时间与采集时间各自独立。[逐项与入口](inventory.json) · [真实数据库对象与验证](database.json) · [供应商字段使用明细](../research/investmind-tushare-field-consumers.json) · [非 Tushare 字段](../research/investmind-non-tushare-field-usage.json)

## 证券与股票（81 项）

| 条目 | 归属与独立查询 | 本机取证 | 处置与范围 |
| --- | --- | --- | --- |
| 复权因子 `stock-adjustment` | 公共数据集。`stock-adjustment` | 37,029 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 资产负债表 `stock-balance` | 公共数据集。`stock-financial-reports` | 784 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 北交所证券代码映射 `stock-bse-mapping` | 保留专项 | 1 条输入捕获 | 北交所新旧代码的证据与组合别名核验绑定；按证券身份消费。 |
| 现金流量表 `stock-cashflow` | 公共数据集。`stock-financial-reports` | 915 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 股票日行情 `stock-daily` | 公共数据集。`stock-daily` | 36,989 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 每日估值与股本 `stock-daily-basic` | 公共数据集。`stock-daily-basic`、`stock-limit-status` | 36,989 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 财报披露日程 `stock-disclosure` | 公共事实及内部证据。`stock-disclosures` | 598 条主表记录 | 披露日历按报告期更新，旧的预定和修改日期无法完整回溯。 对经合同登记的字段提供库存查询。 |
| 分红送转记录 `stock-dividend` | 公共数据集。`stock-dividends` | 528 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 业绩快报 `stock-express` | 公共事实及内部证据。`stock-company-events` | 59 条主表记录 | 快报稀疏，留有当期库存及字段证据；与正式报告分别解释。 对经合同登记的字段提供库存查询。 |
| 财务指标 `stock-financial-indicator` | 公共数据集。`stock-financial-reports` | 722 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 主营业务分部 `stock-business` | 保留现有领域能力 | 283 条主表记录 | 分部收入需按产品/地区口径拆分，专项补充名称与收入并带原件核验。 |
| 业绩预告 `stock-forecast` | 公共事实及内部证据。`stock-company-events` | 110 条主表记录 | 预告的上下限、预告类型和原因由业绩披露合同处理；保留专项详情的合并规则。 对经合同登记的字段提供库存查询。 |
| 利润表 `stock-income` | 公共数据集。`stock-financial-reports` | 753 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 行业分类 `stock-classification` | 公共数据集。`stock-classification-members` | 870 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 行业成员 `stock-classification-members` | 公共数据集。`stock-classification-members` | 50 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 股份回购 `stock-repurchase` | 公共事实及内部证据。`stock-company-events` | 602 条主表记录 | 回购计划与实际执行由进度和公告区分；继续供应历史资本回报计算。 对经合同登记的字段提供库存查询。 |
| 限售股解禁 `stock-unlock` | 公共事实及内部证据。`stock-company-events` | 82 条主表记录 | 原件与 Core 供历史股本事件核对；公告日、流通日和旧 unlock_date 不同，保留各自来源。 对经合同登记的字段提供库存查询。 |
| 股东户数 `stock-holder-count` | 保留现有领域能力 | 1,333 条主表记录 | 户数按报告期留存；部分原公告时间字符串未写入标准日期，保留原件。 |
| 证券基础资料 `stock-basic` | 公共数据集。`stock-securities` | 5,565 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 前十大股东 `stock-top-holders` | 保留现有领域能力 | 6,873 条主表记录 | 同一股东多期记录与机构分类用于持仓分析；股东类别由专项合同解释。 |
| 股票交易日历 `trade-calendar` | 公共数据集。`stock-trade-calendar` | 2,355 条主表记录 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 真钱指数 `metric-realMoneyIndex` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 自由现金流 `metric-freeCashFlow` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 历史投入资本回报率 `metric-roic` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 历史市盈率分位 `metric-pePercentile` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 历史市净率分位 `metric-pbPercentile` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 连续分红年数 `metric-dividendYears` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 派息率 `metric-payoutRatio` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 股东回报率 `metric-capitalReturnRatio` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 历史预期股息率 `metric-expectedDividendYield` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 机构持仓比例 `metric-institutionHolding` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 净负债 `metric-netDebt` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 净利润同比增长 `metric-profitGrowth` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 营业收入同比增长 `metric-revenueGrowth` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| 经营现金流同比增长 `metric-cashflowGrowth` | 保留历史派生结果 | 202 条指定条件记录 | 保留登记公式、计算日期、结果和输入。当前选股同名指标和工作台卡片按各自公式发布，独立查询历史公式暂用带版本派生表。 |
| PE (TTM) `card-workbenchPeTtm` | InvestMind 产品或适配 | 100 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| PB `card-workbenchPb` | InvestMind 产品或适配 | 100 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 总市值 `card-workbenchMarketCap` | InvestMind 产品或适配 | 100 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 账上净现金 `card-workbenchNetCash` | InvestMind 产品或适配 | 100 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 股息率 `stock-dividend-yield` | InvestMind 产品或适配 | 300 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 分红次数(近5财年) `card-workbenchFiveYearDividendCount` | InvestMind 产品或适配 | 100 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 存贷双高 `risk-high_cash_high_debt` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 商誉/资产 `risk-goodwill_risk` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 非经营依赖度 `risk-profit_structure_non_operating_dependency` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 减值压力 `risk-impairment_pressure` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 综合收益差异 `risk-comprehensive_income_gap` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| EPS 质量差 `risk-eps_quality_gap` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 净资产现金流支撑 `risk-equity_cash_support` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 估值分位带 `risk-valuation_band` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 有息负债负担 `risk-interest_debt_burden` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 分红与融资 `risk-dividend_vs_financing` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 分红可持续性 `risk-dividend_sustainability` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 利润结构/扣非 `risk-earnings_quality_non_recurring` | InvestMind 产品或适配 | 50 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 比率分析输入 `stock-ratio-inputs` | InvestMind 产品或适配 | 753 条主表记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 证券配对研究 `stock-research` | InvestMind 产品或适配 | 2,121 条输入捕获 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 证券搜索 `security-search` | InvestMind 产品或适配 | 58 条输入捕获 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 证券地域 `security-region` | 保留现有领域能力 | 50 条指定条件记录 | 证券地区只在专项标注来源后供市场热力图聚合；公共证券查询目前不承诺地区字段。 |
| 证券公告 `stock-announcements` | 公共事实及产品适配。`content-documents` | 8 条指定条件记录 | 公开可展示公告的身份、时间和链接；图表标记、回填和留存清理由产品负责。 |
| PE · 市盈率 `screener-pe` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| PB · 市净率 `screener-pb` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| PE分位 · 5年 `screener-pePercentile` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| PB分位 · 5年 `screener-pbPercentile` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 连续分红 · 年数 `screener-dividendYears` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 股利支付率 · 3年均值 `screener-payoutRatio` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 资本回报/股权筹资(FY) · 分红+回购/股权筹资 `screener-capitalReturnRatio` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| ROE · 净资产收益率 `screener-roe` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 年报 ROIC · 投入资本回报率 `screener-roic` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 真钱指数 · 现金流/归母净利润 `screener-realMoneyIndex` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 经现/营收 · FY年报 `screener-ocfToRevenue` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 经现/负债 · FY年报 `screener-ocfToTotalLiab` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 资本开支保障 · A股 OCF/capex `screener-capexCoverageRatio` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 自由现金流 · A股 OCF-capex `screener-freeCashFlowFy` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 自由现金流率 · A股 OCF-capex/营收 `screener-freeCashFlowMarginFy` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 收现比 · A股现金流明细 `screener-cashCollectionRatio` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 付现比 · A股现金流明细 `screener-cashPaymentRatio` | InvestMind 产品或适配 | 500 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 机构持仓 · 前十大流通股东合计 `screener-institutionHolding` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 机构增减 · 较上期变化 `screener-institutionHoldingChange` | InvestMind 产品或适配 | 450 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 长钱持仓 · 国家队/社保/险资/QFII `screener-longMoneyHoldingRatio` | InvestMind 产品或适配 | 0 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 基金重仓数 · 公募重仓 `screener-fundHeavyCount` | InvestMind 产品或适配 | 0 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 筹码集中 · 股东户数下降 `screener-chipConcentrationChange` | InvestMind 产品或适配 | 0 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 市值 · 亿元 `screener-marketCap` | InvestMind 产品或适配 | 6,356 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |

## 市场、指数、ETF 与商品（17 项）

| 条目 | 归属与独立查询 | 本机取证 | 处置与范围 |
| --- | --- | --- | --- |
| 市场每日统计 `market-daily-summary` | 保留专项 | 4 条输入捕获 | 交易所总貌供宏观流通市值等汇总。统计范围、发布时间独立于逐股行情。 |
| ETF 基础资料 `etf-basic` | 保留专项 | 0 条指定条件记录 | 未留存该 API 样本，配置与基金日行情、份额是不同对象。 |
| ETF 日行情 `etf-daily` | 公共数据集。`etf-trading`、`market-daily` | 18 条输入捕获 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 基金份额与规模 `etf-shares` | 公共事实及内部证据。`etf-trading` | 9 条输入捕获 | 开放具备完整来源的已核验交易数据及其来源引用；供给目录及逐次专项证据保持内部。 |
| 期货合约资料 `commodity-contracts` | 保留专项 | 10 条输入捕获 | 商品日行情使用经过校验的合约及报价单位；全合约目录不按行情端点开放。 |
| 商品期货日行情 `commodity-daily` | 公共事实及内部证据。`commodity-daily` | 33 条输入捕获 | 开放具备完整来源的已核验交易数据及其来源引用；供给目录及逐次专项证据保持内部。 |
| 期货主力合约映射 `commodity-mapping` | 公共事实及内部证据。`commodity-daily` | 10 条输入捕获 | 开放具备完整来源的已核验交易数据及其来源引用；供给目录及逐次专项证据保持内部。 |
| 期货交易日历 `commodity-calendar` | 保留专项 | 4 条输入捕获 | 主力合约窗口要对照完整期货开市日历；保留日历作为逐日商品数据的完整性证据。 |
| 外汇日行情 `fx-daily` | 保留专项 | 4 条输入捕获 | USD/CNH 的 bid_close 通过宏观专项查询；其他货币对、价侧和日期范围保留原件。 |
| 指数基础资料 `index-basic` | 保留专项 | 2 条输入捕获 | 基准目录按代码、全收益口径与组合清单核验，保留可追溯的识别凭证。 |
| 国内指数日行情 `index-daily` | 公共数据集。`market-daily` | 5 条输入捕获 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 全球指数日行情 `index-global` | 公共数据集。`market-daily` | 3 条输入捕获 | 按带业务日期和来源的领域事实读取；更正和空值按相应数据集合同解释。 |
| 指数成分与权重 `index-weights` | 保留专项 | 0 条指定条件记录 | 未留存该 API 的权重原件，目录只证明来源合同。 |
| 融资融券余额 `market-margin` | 保留专项 | 6 条输入捕获 | 两融余额按交易所及交易日采集，专题使用可比的流通市值分母。 |
| 深圳市场每日统计 `market-sz-summary` | 保留专项 | 1 条输入捕获 | 深交所总貌由对应交易所日历与单位校准后供专题使用。 |
| 市场统计与情绪 `market-overview` | 公共事实及产品适配。`stock-limit-status` | 1 条指定条件记录 | 涨跌停状态可逐股查询。市场宽度、榜单和市场表现按概览版本发布。 |
| 市场浏览与热力图 `market-explorer` | InvestMind 产品或适配 | 1 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |

## 宏观（39 项）

| 条目 | 归属与独立查询 | 本机取证 | 处置与范围 |
| --- | --- | --- | --- |
| 中国实际 GDP 同比 `cn.gdp.real_yoy` | 公共数据集。`macro-observations` | 40 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国 CPI 同比 `cn.cpi.yoy` | 公共数据集。`macro-observations` | 120 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国核心 CPI 同比 `cn.cpi.core_yoy` | 公共数据集。`macro-observations` | 122 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国 PPI 同比 `cn.ppi.yoy` | 公共数据集。`macro-observations` | 120 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国制造业 PMI `cn.pmi.manufacturing` | 公共数据集。`macro-observations` | 120 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国规模以上工业增加值同比 `cn.industry.value_added_yoy` | 公共数据集。`macro-observations` | 102 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国 M1 同比 `cn.money.m1_yoy` | 公共数据集。`macro-observations` | 120 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国 M2 同比 `cn.money.m2_yoy` | 公共数据集。`macro-observations` | 120 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国社会融资规模存量同比 `cn.credit.tsf_stock_yoy` | 公共数据集。`macro-observations` | 0 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国 DR007 `cn.rate.dr007` | 公共数据集。`macro-observations` | 2,504 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 中国 10 年期国债收益率 `cn.rate.govbond_10y` | 公共数据集。`macro-observations` | 2,504 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美元兑人民币汇率 `cn.fx.usdcny` | 公共数据集。`macro-observations` | 0 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国实际 GDP 环比折年率 `us.gdp.real_qoq_annualized` | 公共数据集。`macro-observations` | 231 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国 CPI 同比 `us.cpi.yoy` | 公共数据集。`macro-observations` | 553 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国核心 CPI 同比 `us.cpi.core_yoy` | 公共数据集。`macro-observations` | 553 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国核心 PCE 同比 `us.pce.core_yoy` | 公共数据集。`macro-observations` | 940 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国失业率 `us.labor.unemployment_rate` | 公共数据集。`macro-observations` | 201 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国非农就业月增量 `us.labor.nonfarm_payroll_change` | 公共数据集。`macro-observations` | 886 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国有效联邦基金利率 `us.rate.effective_fed_funds` | 公共数据集。`macro-observations` | 3,673 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 美国 10 年期国债收益率 `us.rate.treasury_10y` | 公共数据集。`macro-observations` | 2,512 条指定条件记录 | 宏观观测按序列、来源、观测期、公布时刻、修订号选版；产品负责传导、展示与状态。 |
| 国内生产总值原始数据 `source-cn_gdp` | 内部证据 | 1 条输入捕获 | 同源 GDP 原件已映射到观测及名义 GDP 专题，保留来源版本及字段口径。 |
| 离岸人民币汇率 `macro-extension-usdcnh` | 公共事实及产品适配。`macro-specialty-observations` | 5 条指定条件记录 | 提供已完成任务且与 Core 核对一致的观测。增长率、汇率变化等产品计算保留在专题。 |
| 美国波动率指数 `macro-extension-vix` | 保留专项 | 1 条指定条件记录 | 专题保留 CBOE 来源及 FRED 分发归属和用途限制；在专题内展示有来源的观测。 |
| 政府债券融资 `macro-extension-governmentBonds` | 保留专项 | 2 条指定条件记录 | 官方社会融资规模增量表按月份和资料版次留存；缺文件的月份保留缺口。 |
| 住户中长期贷款 `macro-extension-householdCredit` | 保留专项 | 5 条指定条件记录 | 贷款统计报告按文档及季末兜底选取；抓取失败与真实零值须区分。 |
| 一线城市住宅价格 `macro-extension-tier1Housing` | 保留专项 | 95 条指定条件记录 | 70 城报告按城市、基期和价格口径核对；目录与报告有独立凭证。 |
| 地方政府专项债 `macro-extension-specialBonds` | 保留专项 | 4 条指定条件记录 | 预算限额、发行与余额的表头、期间及计量口径分别留证。 |
| 房地产贷款 `macro-extension-propertyLoans` | 保留专项 | 1 条指定条件记录 | 季度贷款投向报告的范围和同比增速随报告定义；保留官方文档依据。 |
| 融资余额 `macro-extension-marginBalances` | 保留专项 | 6 条指定条件记录 | 两融余额与流通市值跨所对齐，在指定发布滞后后计算比率。 |
| A 股流通市值 `macro-extension-aShareFloatMv` | 保留专项 | 4 条指定条件记录 | 沪深两市需同日且单位一致；未证实的一侧不可填零。 |
| 名义 GDP 与平减指标 `macro-extension-nominalGdp` | 公共事实及产品适配。`macro-specialty-observations` | 2 条指定条件记录 | 提供已完成任务且与 Core 核对一致的观测。增长率、汇率变化等产品计算保留在专题。 |
| 企业样本与官方指数、ETF 成分范围 `macro-enterprise` | InvestMind 产品或适配 | 1 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| 宏观利差与剪刀差 `macro-spreads` | InvestMind 产品或适配 | 1 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |
| PPI-CPI 剪刀差 `cn.ppi_cpi_spread` | InvestMind 产品或适配 | 37 条指定条件记录 | 差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。 |
| M2-M1 剪刀差 `cn.m2_m1_spread` | InvestMind 产品或适配 | 37 条指定条件记录 | 差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。 |
| M1-M2 差值 `cn.money.m1_minus_m2_yoy` | InvestMind 产品或适配 | 37 条指定条件记录 | 差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。 |
| 国债-DR007 利差 `cn.govbond_dr007_spread` | InvestMind 产品或适配 | 51 条指定条件记录 | 差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。 |
| 美债-联邦基金利差 `us.treasury_fed_funds_spread` | InvestMind 产品或适配 | 38 条指定条件记录 | 差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。 |
| 中美利差 `cn.us.rate_spread` | InvestMind 产品或适配 | 37 条指定条件记录 | 差值按已登记的输入和历史选版在产品计算；保留公式版本、单位、交集日期和缺口。 |

## 财经内容及导入（3 项）

| 条目 | 归属与独立查询 | 本机取证 | 处置与范围 |
| --- | --- | --- | --- |
| 财经快讯 `news` | 公共事实及产品适配。`content-documents` | 10,808 条指定条件记录 | 共享已获展示许可的库存文档；中文来源、展示时间、采集健康及旧游标由产品负责。 |
| 宏观文件导入 `external-macro` | 内部证据 | 2 条指定条件记录 | 文件及逐行导入回执。通过已规范化的宏观观测或可展示文档对外提供事实。 |
| 公开内容文件导入 `external-content` | 公共事实及内部证据。`content-documents` | 10,831 条指定条件记录 | 可展示导入文档按内容合同读取。导入源文件、原始内容和逐行校验属于内部。 |

## 组合公共输入（1 项）

| 条目 | 归属与独立查询 | 本机取证 | 处置与范围 |
| --- | --- | --- | --- |
| 资产价格、分红与基准公共输入 `portfolio-inputs` | InvestMind 产品或适配 | 1 条指定条件记录 | 按已发布版本、公式、筛选或搜索规则读取。保留产品合同和原件、事实、派生值之间的来源链。 |

## 数据库反查

以下对象均来自同一只读快照。每张表的物理列、索引、约束、键检查、模型、引用它的源码、迁移及目录反向索引保存在 `database.json → relations`。源码引用为对全限定表名及 ORM 类名的反查，动态生成的 SQL 另看该项所列业务代码。行数按精确计数。关系未在目录中独列时，在下表标识用途。

| 关系 | 记录 | 检查的键 | 目录关联 | 用途 |
| --- | ---: | --- | ---: | --- |
| `core.adjustment_factor` | 37,029 | `ts_code, trade_date` | 12 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.balance_sheet` | 784 | `ts_code, end_date, report_type, ann_date, update_flag` | 12 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.business_segment` | 283 | `ts_code, end_date, segment_name` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.cashflow_statement` | 915 | `ts_code, end_date, report_type, ann_date, update_flag` | 21 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.classification` | 870 | `provider, source, classification_id` | 4 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.classification_member` | 50 | `classification_id, ts_code, effective_from` | 4 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.content_document` | 10,816 | `dedupe_key` | 4 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.daily_basic` | 36,989 | `ts_code, trade_date` | 18 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.daily_price` | 36,989 | `ts_code, trade_date` | 11 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.disclosure_schedule` | 598 | `ts_code, end_date` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.dividend` | 528 | `ts_code, end_date, ann_date, div_proc` | 14 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.enterprise_order_supplement` | 470 | `request_key` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.enterprise_rea_supplement` | 981 | `request_key` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.enterprise_receivable_supplement` | 470 | `request_key` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.enterprise_target_candidate` | 1 | `observation_id, normalizer_version, policy_hash, as_of, timezone` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.financial_express` | 59 | `ts_code, end_date, report_type, ann_date, update_flag` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.financial_forecast` | 110 | `ts_code, end_date, report_type, ann_date, update_flag` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.financial_indicator` | 722 | `ts_code, end_date, report_type, ann_date, update_flag` | 14 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.income_statement` | 753 | `ts_code, end_date, report_type, ann_date, update_flag` | 24 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.index_constituent` | 0 | `index_code, ts_code, effective_from` | 0 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.listing_status_history` | 0 | `ts_code, effective_from, status` | 0 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.macro_observation` | 15,674 | `source, series_id, observed_at, released_at, revision` | 39 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.market_daily_quote` | 114 | `provider, asset_type, asset_code, trade_date` | 6 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.price_limit` | 0 | `ts_code, trade_date` | 0 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.repurchase` | 602 | `ts_code, ann_date, proc` | 4 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.security` | 5,565 | `ts_code` | 10 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.security_name_history` | 0 | `ts_code, start_date, end_date, name` | 0 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.share_unlock` | 82 | `ts_code, ann_date` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.shareholder_count` | 1,333 | `ts_code, end_date` | 1 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.suspension` | 0 | `ts_code, trade_date` | 0 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.top_holder` | 6,873 | `ts_code, end_date, holder_name` | 5 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `core.trade_calendar` | 2,355 | `exchange, cal_date` | 5 | 标准事实与领域选版依据；保留可追溯的来源引用和历史字段 |
| `derived.macro_xray_result` | 0 | `id` | 0 | InvestMind 计算结果、公式版本及可重放血缘；独立于原始事实保存 |
| `derived.metric_input` | 25,905 | `id` | 15 | InvestMind 计算结果、公式版本及可重放血缘；独立于原始事实保存 |
| `derived.metric_value` | 3,628 | `ts_code, metric_id, as_of, formula_version` | 20 | InvestMind 计算结果、公式版本及可重放血缘；独立于原始事实保存 |
| `derived.risk_result` | 600 | `ts_code, risk_id, as_of` | 12 | InvestMind 计算结果、公式版本及可重放血缘；独立于原始事实保存 |
| `mart.page_snapshot` | 1,879 | `product, snapshot_version, history_generation` | 43 | InvestMind 发布及历史重放；保持版本、摘要和回退读取 |
| `mart.published_snapshot` | 56 | `product` | 30 | InvestMind 发布及历史重放；保持版本、摘要和回退读取 |
| `mart.stock_daily_chart` | 860,357 | `ts_code, trade_date, snapshot_version` | 0 | InvestMind 发布及历史重放；保持版本、摘要和回退读取 |
| `mart.stock_screener_daily` | 6,356 | `ts_code, as_of, snapshot_version` | 24 | InvestMind 发布及历史重放；保持版本、摘要和回退读取 |
| `mart.stock_workbench_snapshot` | 1,592 | `ts_code, snapshot_version` | 0 | InvestMind 发布及历史重放；保持版本、摘要和回退读取 |
| `ops.data_acquisition_state` | 487 | `entry_id, item_id, subject_kind, subject_key, scope_kind` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.enterprise_order_attempt` | 470 | `id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.enterprise_rea_attempt` | 981 | `request_key` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.enterprise_rea_plan` | 1 | `sequence_number` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.enterprise_receivable_attempt` | 470 | `id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.enterprise_target_execution_attempt` | 2 | `request_key` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.enterprise_target_execution_plan` | 1 | `fingerprint` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.job_cursor` | 2,252 | `job_name, cursor_key` | 2 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.job_failure` | 111 | `id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.job_run` | 452 | `id` | 10 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.local_admin_execution` | 0 | `id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.local_admin_task` | 9 | `name` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.macro_series_state` | 20 | `series_id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.news_delivery` | 10,822 | `id` | 1 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.operational_alert` | 1 | `alert_key` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.quality_check_result` | 1,469 | `id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.scheduler_checkpoint` | 0 | `checkpoint_key` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `ops.scheduler_heartbeat` | 0 | `instance_id` | 0 | 内部覆盖、任务、治理或运维；保留执行及查询凭证 |
| `public.alembic_version` | 1 | `version_num` | 0 | 内部数据库迁移版本 |
| `raw.enterprise_order_response` | 470 | `attempt_id` | 1 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.enterprise_rea_response` | 981 | `request_key` | 1 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.enterprise_receivable_response` | 470 | `attempt_id` | 1 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.enterprise_target_observation` | 1 | `observation_id` | 1 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.external_file` | 251 | `file_hash` | 2 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.external_record` | 10,833 | `external_file_id, row_number` | 0 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.ingestion_batch` | 4,025 | `id` | 0 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.rejected_record` | 1,759 | `id` | 0 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |
| `raw.source_payload` | 4,004 | `batch_id, request_hash` | 111 | 内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责 |

## 作业和路由反查

代码注册了 93 个 CLI 命令和 11 个调度作业。逐个名称、实现模块和触发器位于 `inventory.json → cliCommands / schedulerJobs`。实际批次的来源、数据集、模式、结果以及用到的 API 见 `database.json → comparisons.rawTaskOwnership`，历史任务名、终态、时间范围及游标另列于 `actualRuns / cursorJobs`。CLI 注册与调度配置不能证明任务本机已运行。

| 路由 | 方法 | 运行职责 |
| --- | --- | --- |
| `/openapi.json` | GET, HEAD | 服务元数据 |
| `/docs` | GET, HEAD | 服务元数据 |
| `/docs/oauth2-redirect` | GET, HEAD | 服务元数据 |
| `/redoc` | GET, HEAD | 服务元数据 |
| `/health` | GET | 服务元数据 |
| `/api/v1/data-status` | GET | 采集状态 |
| `/api/v1/news` | GET | 产品及查询 |
| `/api/data/market-news` | GET | 兼容产品 |
| `/api/v1/datasets/content-documents` | GET | 独立领域事实 |
| `/api/v1/datasets/market-daily` | GET | 独立领域事实 |
| `/api/v1/datasets/etf-trading` | GET | 独立领域事实 |
| `/api/v1/datasets/commodity-daily` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-limit-status` | GET | 独立领域事实 |
| `/api/v1/datasets/macro-observations` | GET | 独立领域事实 |
| `/api/v1/datasets/macro-specialty-observations` | GET | 独立领域事实 |
| `/api/v1/market/overview` | GET | 产品及查询 |
| `/api/data/market-overview` | GET | 兼容产品 |
| `/api/v1/market/explorer` | GET | 产品及查询 |
| `/api/data/market-explorer` | GET | 兼容产品 |
| `/api/v1/macro/transmission` | GET | 产品及查询 |
| `/api/data/macro-transmission` | GET | 兼容产品 |
| `/api/v1/macro/insights` | GET | 产品及查询 |
| `/api/data/macro-insights` | GET | 兼容产品 |
| `/api/v1/portfolio` | GET | 产品及查询 |
| `/api/data/portfolio` | GET | 兼容产品 |
| `/api/v1/stocks/research` | POST | 产品及查询 |
| `/api/data/stocks/research` | POST | 兼容产品 |
| `/api/v1/stocks/screener` | POST | 产品及查询 |
| `/api/data/screener` | POST | 兼容产品 |
| `/api/v1/stocks/{symbol}` | GET | 产品及查询 |
| `/api/data/stocks/{symbol}` | GET | 兼容产品 |
| `/api/v1/datasets/stock-daily` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-adjustment` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-daily-basic` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-securities` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-trade-calendar` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-classification-members` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-financial-reports` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-disclosures` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-company-events` | GET | 独立领域事实 |
| `/api/v1/datasets/stock-dividends` | GET | 独立领域事实 |
| `/api/v1/search` | GET | 产品及查询 |
| `/api/data/search` | GET | 兼容产品 |
| `/admin/` | GET | 本机管理 |
| `/admin/api/session` | GET | 本机管理 |
| `/admin/docs/{name:path}` | GET | 本机管理 |
| `/admin/api/data-status` | GET | 本机管理 |
| `/admin/api/tables` | GET | 本机管理 |
| `/admin/api/catalog` | GET | 本机管理 |
| `/admin/api/catalog-runtime` | GET | 本机管理 |
| `/admin/api/catalog/{entry_id}` | GET | 本机管理 |
| `/admin/api/tables/{schema}/{name}` | GET | 本机管理 |
| `/admin/api/tables/{schema}/{name}/cell` | GET | 本机管理 |
| `/admin/api/config/{name}` | GET | 本机管理 |
| `/admin/api/config/{name}/apply` | POST | 本机管理 |
| `/admin/api/tasks` | GET | 本机管理 |
| `/admin/api/tasks/{name}/runs` | GET | 本机管理 |
| `/admin/api/tasks/{name}/runs/{run_id}` | GET | 本机管理 |
| `/admin/static` | 挂载 | 本机管理 |
