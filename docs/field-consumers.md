# 从公共字段查 InvestMind 的页面与指标

按供应商的接口和字段名，可以在[全部 Tushare 原字段目录](tushare-field-consumers.md)查找对应的数据库、项目页面或任务、指标、历史去留及待核情况。

当前登记的消费项目为 InvestMind。按下表的字段或原字段搜索，可找到直接显示位置，以及用它计算的页面指标。新消费项目可以补充同一字段的用途。选股页面的完整逐列、阈值、预设、散点与银行指标另有[逐字段用途表](screener-field-usage.md)。

| 公共字段或上游原字段 | 项目和页面 | 直接或间接用途及核验 |
| --- | --- | --- |
| stock_basic.ts_code、name | InvestMind 搜索、选股、工作台、市场、组合 | 证券查询用代码和名称匹配并由网站生成链接；其余页面用代码对齐价格、公告、行业、分红和持仓。[证券搜索](security-search.md) |
| daily.close、pct_chg、amount、vol；adj_factor | InvestMind 工作台、选股、市场、组合 | 报价和涨跌、复权日线、市场成交排行与热力图分别使用不同单位和聚合范围。组合收益与基准以同日可比序列计算；DCF 另用未复权当前价和市值。[工作台](workbench-api.md) · [市场](market-supply.md) · [组合](portfolio-split-lineage.md) |
| daily_basic.pe、pe_ttm、pb、total_mv | InvestMind 选股、工作台、市场 | 静态 PE、滚动 PE 分列；选股阈值和图轴、研究比较、市值权重或板块聚合，均按各自估值日和币种读取。[选股](screener-api-fields.md) · [工作台](workbench-api.md) · [市场](market-supply.md) |
| dividend 的实施版本、现金金额与支付日 | InvestMind 选股、工作台、组合 | 财年实派、预测股息率和组合持有期到账按不同日期归属；三年平均支付率与最近财年支付率分别计算。[选股](screener-history-evidence.md) · [工作台](workbench-api.md) · [组合](portfolio-split-lineage.md) |
| income.n_income_attr_p、cashflow.n_cashflow_act、c_pay_acq_const_fiolta | InvestMind 选股和工作台 | 财年现金质量、支付率、累计经营现金流/归母利润、严格及维持资本支出 DCF；窗口及年报公开时点影响可得性。[选股现金指标](screener-fiscal-cash.md) · [工作台面板](research/investmind-workbench-panels.md) |
| fina_indicator.roe、roic | InvestMind 选股和工作台 | 收益率明细逐报告展示；选股 ROIC 按最近八个财年内最新有效的披露百分数比较。[字段与历史](screener-history-evidence.md) |
| top10_holders、stk_holdernumber | InvestMind 选股和工作台 | 机构持仓比例、股东户数、变化与股东回报面板。持股占比与户数的报告期、冲突规则分别核验。[选股历史](screener-history-evidence.md) · [工作台面板](research/investmind-workbench-panels.md) |
| index_member_all、stock_basic.area | InvestMind 选股、市场 | 成员有效日期决定申万行业分类，地区采用证券资料。市场按成员聚合涨跌、成交和市值，选股只读对应证券的标签。[分类及原件](screener-history-evidence.md) · [热力图](market-supply.md) |
| fund_daily、fund_share；fut_daily、fut_mapping | InvestMind 市场 | ETF 收盘、涨跌及规模；商品主力映射与日周月 K。证券池行情、基金及期货可能使用不同交易日。[市场供给](market-supply.md) |
| content_document.id、copyright_status、securities、published_at | InvestMind 市场、工作台 | 快讯列表与游标、证券提及次数、最新提及时刻。可见权限控制摘要和正文，证券公告再关联行情图上的事件。[快讯](news-api.md) · [公告与披露](workbench-announcements.md) |
| macro_observation 的系列、期间、vintage；enterpriseAnalysis.inputHash | InvestMind 宏观 | 实际与名义指标、利差、剪刀差和企业对照。宏观首屏与延迟范围按版本和输入哈希拼接。[逐系列及派生口径](macro-supply.md) |
| portfolio.prices、benchmarks、dividendLedger、attributionInputs、treasuryReference | InvestMind 全站组合抽屉 | 净值、时间区间、股息、归因与效率分析。股数、权重、交易以及用户策略由网站管理。[组合分析](portfolio-split-lineage.md) |

研究比较接口已有重放证据，当前页面未确认有直接调用。数据库的校验、修订处理、原件溯源和历史重算还会产生间接用途；未在本表列出的字段须完成全站与底座反查后才可停采。
