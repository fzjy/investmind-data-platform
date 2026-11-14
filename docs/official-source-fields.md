# 官方数据、公开内容及文件导入字段索引

InvestMind 当前发布版本 `20261108-366f83bfc727036c`。本索引从原生字段及文件追到接口、页面和指标。20 个基础系列、10 个扩展组、14 个官方目标和两个文件导入合同分别计数。官方目标是企业样本的筛选维度。库内最新内容计数以本次只读核验时刻为准。[逐系列 DTO 路径、配置来源及只读计数](research/investmind-non-tushare-field-usage.json)

## 基础宏观原字段

每行按配置优先级列候选来源、原始系列/字段和转换。实际来源、期数及采集状态取已发布版本和既有采集回执。配置中 7 个系列的实供为 Tushare，11 个为其他来源，2 个当前暂停；美国长债还配置 Tushare 为第三顺位。相同系列可有多个候选。20 系列的聚合读取计划为每周一上海时间 09:00，日、月、季频是观测期，回叠窗口为 31 天、18 个月、12 季。配置已就绪和指定版本实测数值分别留档。[周期、滞后和修订回执](macro-supply.md)

| 系列及指标 | 频率 / 单位 | 候选源及原生字段 | 本版实供 / 期数 / 采集状态 | 引用该系列的发布利差或剪刀差 |
| --- | --- | --- | --- | --- |
| `cn.gdp.real_yoy` 中国实际 GDP 同比 | 季 / % | Tushare P1 cn_gdp · gdp_yoy | tushare / 40 / success | 系列目录与来源状态 |
| `cn.cpi.yoy` 中国 CPI 同比 | 月 / % | Tushare P1 cn_cpi · nt_yoy | tushare / 120 / success | cn.ppi_cpi_spread |
| `cn.cpi.core_yoy` 中国核心 CPI 同比 | 月 / % | nbs P1 不包括食品和能源 · 同比涨跌幅（%），ArticleTitle, PubDate，identity。[出处](https://www.stats.gov.cn/sj/zxfb/) | nbs / 121 / success | 系列目录与来源状态 |
| `cn.ppi.yoy` 中国 PPI 同比 | 月 / % | Tushare P1 cn_ppi · ppi_yoy | tushare / 120 / success | cn.ppi_cpi_spread |
| `cn.pmi.manufacturing` 中国制造业 PMI | 月 / index | Tushare P1 cn_pmi · pmi010000 | tushare / 120 / success | 系列目录与来源状态 |
| `cn.industry.value_added_yoy` 中国规模以上工业增加值同比 | 月 / % | nbs P1 规模以上工业增加值 · 同比实际增长（%），ArticleTitle, PubDate，identity。[出处](https://www.stats.gov.cn/sj/zxfb/) | nbs / 101 / success | 系列目录与来源状态 |
| `cn.money.m1_yoy` 中国 M1 同比 | 月 / % | Tushare P1 cn_m · m1_yoy | tushare / 120 / success | cn.m2_m1_spread、cn.money.m1_minus_m2_yoy |
| `cn.money.m2_yoy` 中国 M2 同比 | 月 / % | Tushare P1 cn_m · m2_yoy | tushare / 120 / success | cn.m2_m1_spread、cn.money.m1_minus_m2_yoy |
| `cn.credit.tsf_stock_yoy` 中国社会融资规模存量同比 | 月 / % | 目录无来源 | 缺值 / 0 / paused | 系列目录与来源状态 |
| `cn.rate.dr007` 中国 DR007 | 日 / % | Tushare P1 repo_daily · DR007.IB · weight | tushare / 2504 / success | cn.govbond_dr007_spread |
| `cn.rate.govbond_10y` 中国 10 年期国债收益率 | 日 / % | chinamoney P1 SddsIntrRateGovYldHis · CGB-10Y · tenRate，dateString, tenRate，identity。[出处](https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/SddsIntrRateGovYldHis) | chinamoney / 2504 / success | cn.govbond_dr007_spread、cn.us.rate_spread |
| `cn.fx.usdcny` 美元兑人民币汇率 | 日 / CNY per USD | 目录无来源 | 缺值 / 0 / paused | 系列目录与来源状态 |
| `us.gdp.real_qoq_annualized` 美国实际 GDP 环比折年率 | 季 / % | fred P1 series_observations · A191RL1Q225SBEA · value，date, value, realtime_start, realtime_end，identity。[出处](https://api.stlouisfed.org/fred/series/observations) | fred / 39 / success | 系列目录与来源状态 |
| `us.cpi.yoy` 美国 CPI 同比 | 月 / % | fred P1 series_observations · CPIAUCSL · value，date, value, realtime_start, realtime_end，pc1。[出处](https://api.stlouisfed.org/fred/series/observations)<br>bls P2 publicAPI/v1/timeseries/data · CUSR0000SA0 · value，status, Results[].series[].seriesID, Results[].series[].data.year, Results[].series[].data.period, Results[].series[].data.value，pc1。[出处](https://api.bls.gov/publicAPI/v1/timeseries/data/) | fred / 119 / success | 系列目录与来源状态 |
| `us.cpi.core_yoy` 美国核心 CPI 同比 | 月 / % | fred P1 series_observations · CPILFESL · value，date, value, realtime_start, realtime_end，pc1。[出处](https://api.stlouisfed.org/fred/series/observations)<br>bls P2 publicAPI/v1/timeseries/data · CUSR0000SA0L1E · value，status, Results[].series[].seriesID, Results[].series[].data.year, Results[].series[].data.period, Results[].series[].data.value，pc1。[出处](https://api.bls.gov/publicAPI/v1/timeseries/data/) | fred / 119 / success | 系列目录与来源状态 |
| `us.pce.core_yoy` 美国核心 PCE 同比 | 月 / % | fred P1 series_observations · PCEPILFE · value，date, value, realtime_start, realtime_end，pc1。[出处](https://api.stlouisfed.org/fred/series/observations) | fred / 119 / success | 系列目录与来源状态 |
| `us.labor.unemployment_rate` 美国失业率 | 月 / % | fred P1 series_observations · UNRATE · value，date, value, realtime_start, realtime_end，identity。[出处](https://api.stlouisfed.org/fred/series/observations)<br>bls P2 publicAPI/v1/timeseries/data · LNS14000000 · value，status, Results[].series[].seriesID, Results[].series[].data.year, Results[].series[].data.period, Results[].series[].data.value，identity。[出处](https://api.bls.gov/publicAPI/v1/timeseries/data/) | fred / 119 / success | 系列目录与来源状态 |
| `us.labor.nonfarm_payroll_change` 美国非农就业月增量 | 月 / thousand persons | fred P1 series_observations · PAYEMS · value，date, value, realtime_start, realtime_end，chg。[出处](https://api.stlouisfed.org/fred/series/observations)<br>bls P2 publicAPI/v1/timeseries/data · CES0000000001 · value，status, Results[].series[].seriesID, Results[].series[].data.year, Results[].series[].data.period, Results[].series[].data.value，chg。[出处](https://api.bls.gov/publicAPI/v1/timeseries/data/) | fred / 120 / success | 系列目录与来源状态 |
| `us.rate.effective_fed_funds` 美国有效联邦基金利率 | 日 / % | fred P1 series_observations · DFF · value，date, value, realtime_start, realtime_end，identity。[出处](https://api.stlouisfed.org/fred/series/observations)<br>nyfed P2 rates/unsecured/effr/search · EFFR · percent，refRates.type, refRates.effectiveDate, refRates.percent, refRates.revisionIndicator，identity。[出处](https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json) | fred / 3673 / success | us.treasury_fed_funds_spread |
| `us.rate.treasury_10y` 美国 10 年期国债收益率 | 日 / % | fred P1 series_observations · DGS10 · value，date, value, realtime_start, realtime_end，identity。[出处](https://api.stlouisfed.org/fred/series/observations)<br>us_treasury P2 daily_treasury_yield_curve · BC_10YEAR · BC_10YEAR，NEW_DATE, BC_10YEAR，identity。[出处](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml)<br>Tushare P3 us_tycr · y10 | fred / 2512 / success | us.treasury_fed_funds_spread、cn.us.rate_spread |

国家统计局的 `ArticleTitle/PubDate` 用于定位公布期，取公告正文里相应中文统计项。中国货币网按交易日取十年利率。FRED 的 `date` 是观测日，`realtime_start/realtime_end` 约束版本有效时段。`identity` 直取，`pc1` 用同一可见版本的当月值除以上年同月值，减 1 后乘 100，`chg` 使用同版本相邻月变化。失业率、季度环比折年增速直接取原系列。备用 BLS、纽约联储及美国财政部在表内标明，所列发布版未选中它们。18 个有值系列的最近一期已对原件，全历史按 core 行逐点对账；2 个无来源系列的报告为空，实际采集码为 `MACRO_NO_SOURCE`。[逐点与最新原件对账](research/investmind-macro-supply.json)

来源批次和冻结响应存 `raw.ingestion_batch`、`raw.source_payload`；`core.macro_observation` 用 `source/series_id/observed_at/released_at/revision` 标识一版观测，保存 `value/unit/valid_until/source_url`。同一来源/观测期允许存多版，按读取时刻可见的披露时间、修订和有效期取值。对应产品的 `mart.published_snapshot` 指向当前版，`mart.page_snapshot` 保留历史。InvestMind 的 `/macro` 通过 `/api/v1/macro/insights` 的 `sandboxIndicators[].values/trendDefinitions[].series/seriesStatus[]` 获取基础曲线、指标目录和状态；传导读取 `/api/v1/macro/transmission`。网站按 `createBaselineInsights` 实际选取，经共享图表目录还可查询系列。经济罗盘同时用工业增加值同比与 PPI，跨市场利差只能取两侧均有值的日期。JSON 索引的路径表示当前发布对象确实引用该 ID；网站首页图表的可见字段还取决于投影和用户选择。[展示与公式](macro-supply.md)

## 扩展与专题

配置 `backend/config/macro-extensions.json` 的 10 组均启用计划。表中定时指上海时间的计划。发布扩展组读取自身作业、原件及允许使用的 Core 绑定；部分政府债券值由原始表重解析，回执中的首份文件 `coreCount=0`。保留修订/出处及旧发布快照；失败的尝试记录状态，缺历史点继续为空。公式、具体原件、日期和最近失败按组可查。[专题源表重算](macro-specialties.md) · [逐组原件及缺口](research/investmind-macro-specialties.json)

| 配置组 | 原字段 → 发布字段 | InvestMind 宏观页面用途、采集状态与缺口 |
| --- | --- | --- |
| vix | FRED `VIXCLS` 的 date/value → `extensionInsights.sources.vix` | 外部风险、VIX 日收盘；每日 19:00，发布 129 点 |
| governmentBonds | 人民银行《社会融资规模增量统计表》年月、政府债券月增量、亿元 → 财政分组 | 政府债券融资；每周一 09:00。限 48 月，3 有值，45 缺；五份配置文件仅一份进入本版 |
| householdCredit | 人民币贷款报告“住户贷款…中长期贷款增加/减少”及金额单位（亿元或万亿元）、月份 → `cn.credit.household_medium_long_term_ytd、cn.credit.household_medium_long_term_month` 对应累计与单月系列 | 住户中长期贷款净增；每周一 09:00。累计 2 月，单月 1 月。读取原报告和累计差的版本约束 |
| propertyLoans | 人民银行人民币房地产贷款余额、同比及季度 → `cn.credit.property_loans_balance、cn.credit.property_loans_yoy` 对应两系列 | 房地产贷款余额同比、房地产专题；每周一 09:00。48 月中的 16 个季度均缺正式原件 |
| specialBonds | 财政部年度地方政府专项债务新增限额、当月和累计新增专项债发行额、公布进度 → `special_bonds_new_month/new_ytd/reported_progress` 及额度、推算进度 | 地方专项债发行、财政进度；每周一 09:00。直报月值与跨版本累计差冲突时保留差异，结存额度不进分母 |
| tier1Housing | 国家统计局 70 城报告中一线新建商品住宅、二手住宅销售价格环比及公布月份 → `cn.house.price_70_city_mom.tier1[.existing]` | 房价专题和沙盘一线新房；每周一 09:00。48 月各有 42 点，缺 6 月 |
| usdcnh | Tushare fx_daily.bid_close → 离岸汇价 | 每日 19:00。受控原响应 0 行；网站离岸槽与基础在岸 `cn.fx.usdcny` 各用自身来源 |
| nominalGdp | Tushare cn_gdp.gdp/gdp_yoy → 单季额、近似/严格平减同比 | 每周一 09:00。20 累计季、19 单季额，两种平减各 15 点 |
| aShareFloatMv | Tushare 沪深日汇总 + 深市校验 → 流通市值 | 每日 19:00，融资比例的同交易日分母；48 月有 1 个完整配对 |
| marginBalances | Tushare margin.rzye，交易日/市场 → 沪深融资余额 | 每日 19:00，融资比例分子，同日配对缺 47 月 |

## 官方指数及 ETF 成分

中证文件的日期、指数代码、成份券代码和交易所，基金管理人官方产品页的基金与跟踪指数映射，申赎清单的基金代码、交易日及组合证券代码构成证据。各发行人格式由各自适配器校验；例如 XML 的 `FundInstrumentID/TradingDay/ComponentList/InstrumentID/Quantity/SubstitutionFlag`。它们写入官方成员作业的 `raw.ingestion_batch/source_payload` 和 `ops.job_run/job_cursor` 证据，验证后产出 `enterpriseAnalysis.officialTargets` 的样本日期、成员集合与池内交集。数量、标记和来源链接也用于一致性校验。实际的企业财务金额仍取统一企业会计输入。收益率、ETF 本身持仓权重或历史调仓不能由此推定。[官方成分回执与行业、REA 分支](macro-specialties.md)

| 发布目标 | 原件类别 | 成员日期 | 官方成员 / 50 股池交集 |
| --- | --- | --- | --- |
| 000300.SH 沪深300 | csi-members | 2026-10-23 | 300 / 42 |
| 000905.SH 中证500 | csi-members | 2026-10-23 | 500 / 1 |
| 000510.SH 中证A500 | csi-members | 2026-10-23 | 500 / 41 |
| 512880.SH 证券ETF | csi-members, fund-mapping, fund-pcf | 2026-10-23 | 47 / 3 |
| 512800.SH 银行ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 42 / 3 |
| 159928.SZ 消费ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 36 / 6 |
| 512010.SH 医药ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 20 / 6 |
| 512400.SH 有色金属ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 50 / 2 |
| 512660.SH 军工ETF | csi-members, fund-mapping, fund-pcf | 2026-10-26 | 79 / 0 |
| 515220.SH 煤炭ETF | csi-members, fund-mapping, fund-pcf | 2026-10-26 | 31 / 1 |
| 512980.SH 传媒ETF | csi-members, fund-mapping, fund-pcf | 2026-10-28 | 50 / 0 |
| 515030.SH 新能源车ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 50 / 4 |
| 512200.SH 房地产ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 69 / 0 |
| 588200.SH 科创芯片ETF | csi-members, fund-mapping, fund-pcf | 2026-10-27 | 50 / 2 |

每个目标的官方文件 URL、SHA-256 和公开状态已按代码列在本次 JSON。当前成员回溯的 14 组与企业行业、基础六卡、账本及 REA 的范围分别核验。三个目标与样本池交集为零，保持空值；其余 11 个有交集。源状态以已验证成员文件为准，新一期获取需另有作业证据。[官方文件和每个目标的字段](research/investmind-non-tushare-field-usage.json)

## 内容、权限及原始文件

| 供给 / 原生字段 | 存储和历史 | InvestMind 页面及状态 |
| --- | --- | --- |
| 新浪财经 7×24 `result.status.code/data.feed.list`、`id/is_delete/create_time/rich_text/ext.stocks[].market/symbol` | ID 去重，删除标识/正文格式校验；上海发布时间转 UTC，解析标题、截 160 字、证券映射；只保存元数据与生成的源链接，交付队列 `ops.news_delivery` → `core.content_document`，`fetchedAt/contentHash` 是采集方生成的追踪字段。新闻保留 365 天，日志 90 天 | 市场快讯列表、详情外链、市场页证券提及，`/api/v1/news`、`/api/v1/market/explorer?news=latest`。每 300 秒配置轮询；本次库内 10,808 条均 link_only，摘要及正文皆空。可见页分页和水合复用另有逐页核验。[快讯合同](news-api.md) |
| 巨潮 `code/category/orgId` 验证证券目录；`announcements[].announcementId/secCode/orgId/announcementTitle/announcementTime/adjunctUrl`、`totalAnnouncement/hasMore` | 校验完整分页、总数、证券和编号，保留 `raw.source_payload`、`ops.job_cursor` 窗口。其余 secName、announcementType、adjunctType、replacement、associateAnnouncement 若存在则冻结为来源元数据。归一 `external_id/securities/title/published_at/source_url`，按 ID 更新 `core.content_document` | 个股 `/stocks/[symbol]` 日 K 的公告、定位/覆盖及条件匹配的财报浮层；披露时分未知。配置关闭定期索引，已查两窗、8 份 link_only，北交所未适配。历史保留原件及旧工作台发布。[逐件与未查窗口](workbench-announcements.md) |
| 外部 JSON / JSONL `recordType=macro` | schemaVersion 用于验证，其他合同字段映射 `core.macro_observation`（seriesId→series_id、seasonalAdjustment→seasonal_adjustment、observedAt/releasedAt/sourceUrl 映射对应 snake_case 列），身份、频率、单位和修订随文件记录。只有与成功导入行签名一致的观测可按已验证导入参与选择 | 手工或外部交付，无固定供应商拉取周期；宏观传导和洞察可消费。实际收到 nbs-official-release 两条，2026-08 核心 CPI 同比 1.0%、规模以上工业增加值同比 5.2% 均与页面数值一致；本版取优先级更高的 nbs 原生行，两个导入行保留为来源复核证据 |
| 外部 JSON / JSONL `recordType=content` | source/externalId 或来源+标题+时刻摘要去重；title、publishedAt、sourceUrl、contentType、securities、copyrightStatus 映射内容表；securityNames 辅助消歧，ingestionRevision 留于导入证据，summary/body 按授权裁剪。相同键更新当前内容行 | 导入允许 news、announcement、article。市场快讯和关联证券工作台按产品筛选。当前无 article 记录及已交付文章正文页面，来源文件行数和业务表可见数分开统计 |

两个合同各有 14 个入参，字段全表见 JSON 的 `externalImport.schemaFields`。文件 hash 和处理/拒绝数归 `raw.external_file`；行序、record_type 及经版权处理的 payload 归 `raw.external_record`。完全相同文件幂等。同一文件校验失败不写入业务表。251 个成功文件共 10,833 行（新浪内容 10,831 行、宏观 2 行）；有些已存在内容会按键更新，业务侧还执行保留策略。本轮只读核验不发供应商请求。字段到页面的额外缺口沿用相关专题里的真实状态。

底座根目录运行 `python -I scripts/audit-non-tushare-fields.py` 可重放本回执。脚本验证只读事务、发布版本、两条文件宏观观测最终选源，并从两个发布对象提取逐系列引用。
