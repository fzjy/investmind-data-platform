# 公共数据接口目录

[10 个 v1 接口的请求参数、默认值与校验约束](api-requests.md) · [OpenAPI 规范](openapi-v1.json) · [字段到项目、页面和指标](field-consumers.md)。在底座虚拟环境内运行 `python -I scripts/export-api-requests.py` 可按当前路由重新生成请求合同。


[选股器](screener-api-fields.md) · [工作台](workbench-api.md) · [市场](market-supply.md) · [快讯](news-api.md) · [宏观](macro-supply.md) · [组合](portfolio-split-lineage.md) · [证券搜索](security-search.md)

核验日期：2026-11-14。开工代码基线 `ab00ce213b093472a3647d581519fcdb9a4cb43e`；旧文档范围见 InvestMind 仓库的 `docs/research/investmind-split-reference-scope.md`。这是现有实现目录，不表示独立底座已部署。

## 查询合同

选股查询 `POST /api/v1/stocks/screener` 与兼容入口 `POST /api/data/screener`（`backend/app/stocks/routes.py`），网页入口 `src/modules/stocks/api.ts`。请求含 filters、sortMode、sortRules、page、pageSize；准确约束以 `backend/app/stocks/models.py` 为准。响应含数据与快照 asOf/version；路径中的 v1 标记接口协议，快照版本标记本次查询所用的数据。筛选、排序、分页通过 ScreenerService 和 database_provider 完成。

| 稳定字段标识 | 计算卡 | 公式（百分点） |
| --- | --- | --- |
| dividendYield | workbenchFiscalDividendYield | 快照上一财年已派税前 DPS ÷ 收盘价 × 100 |
| expectedDividendYield | workbenchAnnualDividendYield | 本年预测归母净利 ÷ 总股数 × 加权派息率 ÷ 收盘价 × 100 |
| expectedDividendYieldNTM | workbenchNtmDividendYield | 最新披露季度之后四季的预测归母净利之和 ÷ 总股数 × 加权派息率 ÷ 收盘价 × 100 |

本年预测取当年最新可用中期累计归母净利，除以前五年同报告期/全年利润占比的合格样本算术均值；两端利润须正、占比不超过1。NTM每季为上年同季单季利润×2减前年同季，累计报表转换单季。加权派息率为前两财年 DPS/正年度基本 EPS，较早年权重3、较近年5，再除8。基本 EPS 优先 income.basic_eps，缺失回退 fina_indicator.eps。

## 原字段与依赖反查

| 供应商接口及原字段 | Core 表列 | 直接/间接使用 |
| --- | --- | --- |
| Tushare daily.close/trade_date | core.daily_price.close/trade_date | 三种股息率分母 |
| Tushare daily_basic.total_share | core.daily_basic.total_share，万股换股 | 两种预期 EPS 分母；分红资本基准核验 |
| Tushare dividend.cash_div_tax/end_date/div_proc/pay_date/ann_date/record_date/ex_date | core.dividend 同名列 | 财年 DPS；两种预期派息率 |
| Tushare dividend.imp_ann_date/base_date/base_share/stk_div/stk_bo_rate/stk_co_rate/div_listdate | core.dividend.data 扩展原件字段 | 公告可见性、事件版本、送转基准校验；具体投影须结合账本 |
| Tushare income.n_income_attr_p/basic_eps | core.income_statement.data；ann_date/f_ann_date/end_date/report_type/update_flag 为列 | 两项预测利润与派息率 |
| Tushare fina_indicator.eps | core.financial_indicator.data | 年度基本 EPS 回退，间接影响两项预期 |

计算证据：`backend/app/stocks/derived/workbench_cards.py`，字段映射：`backend/app/stocks/marts/screener_list.py:YIELD_CARDS`。派生模型 `derived.metric_value` 存 value（Numeric(28,10)）、formula_version、as_of、quality、missing_fields、calculation；`derived.metric_input` 可存输入表、记录键和摘要。不能据此推断每次列表查询均读取已落库卡片：列表构建直接调用 calculate_metric_cards。

## 版本、空值与更新

计算 Decimal 精度50，结果保留10位小数；输出为百分点，网页不再乘100。分母非正、必需输入不足、同版冲突、分红覆盖或送转基准未证实、预测亏损时留 null，合法零值保留。财年分红仅计已实施、派息日不晚于 asOf 的正现金事件；完整覆盖证明后无正现金才可为零。

**运行口径。** 快照业务日为 2026-10-21，当前版本实际发布于北京时间 2026-11-14 13:35:53。开发池 50 只股票的三种发布值逐项与同日 derived.metric_value 的 v2 记录相等（含空值）。该日另存 v1 三项各 50 条；查询版本必须同时指定 metric_id、as_of 和 formula_version。页面记录的分红口径为 paid-cash-records-v1。JSON 的 publication.bySymbol 可按股票查看数值、发布缺项原因与 v2 卡片的缺项。财年股息率的唯一空值为 920002.BJ，原因涉及 2026-05-27 除权前后送转基准的可比性。[逐证券和采集证据](research/investmind-dividend-lineage-observed.json)

**历史边界。** Core 覆盖按本次 50 股统计。分红记录的报告期为 2023-06-30 至 2026-06-30，528 行中有 103 个证券/报告期组具多个公告日期；利润表为 2022-12-31 至 2026-09-30，财务指标为 2023-09-30 至 2026-09-30。后两表按证券、报告期、报告类型统计，分别有 6 组和 3 组出现多个公告日期。同一期分红可以有多个方案；多公告日期只证明存在多条观测。相同业务版本键可更新已有 Core 记录。按实际发布时间还原历史需要保留原件，证实每次修订的生效时间及查询当时已可见的输入。当前能定位的选股历史产品覆盖 5 个业务日、21 个版本，其中 2026-10-21 有 11 个版本。资料不足以判定通用的“逐日无前视复现”起始日，本项登记为既有缺口。

**采集节奏与用量。** 当前开发配置为 Asia/Shanghai 每日 19:00 的 sync-daily 触发，股票例行更新开关开启。ops.scheduler_heartbeat 无记录。ops.job_run 中 sync-stock-data 有 15 次运行，分布在 10 月 14、16、21 日，累计请求计数 786。10 月 21 日两次分别为北京时间 19:43 的 268 次和 19:56 的 0 次。这是该任务全部数据集的计数，执行时间的间隔不能证明定时触发。持续调度的实测留至清单 4.2。行情按交易日、财务按公告、分红按方案和实施公告更新；已登记的分红默认 7 日回看属于采集策略。

| Tushare 接口 | 常规同主题数据集历史批次 request_count 合计 | 计数范围 |
|---|---:|---|
| daily | 225 | daily |
| daily_basic | 220 | daily_basic |
| dividend | 221 | dividend，其中 50 个成功 manual 批次计数为 0 |
| income | 70 | income |
| fina_indicator | 70 | financial_indicator |

计数包含上述数据集的 manual、standard、已完成及部分成功或失败记录，涵盖整个开发库，未按证券过滤；一批中可以含多只股票。专题采集中的 daily_basic、dividend、income、fina_indicator 另按 dataset 列在证据文件。job_run 与 ingestion_batch 对同一执行可能均有记账，两层计数不可相加。对某个成功留存原件的批次仍允许供应商请求为零，仓库可复用早期原件。实际去重需要对齐任务身份、证券、窗口及请求哈希。

## 数值基线与缺口

`research/investmind-screener-baseline.json`：快照 `20261021-1c01ff6eb0b7d78a`，asOf 2026-10-21。全板块默认50行，三项 null 分别1/8/11；开启 dividendYield>=0 后41行，null 为0/2/5。两组有序行 SHA256 分别 `18760ca76a1daa7d97d7426526f07ee73ca2142d2e50cdd160981631a47bc2fa`、`376995d24708644a3637911519167e783d53466578e7085aa1a3d1d19e970c7e`。默认页200各1 HTTP/1 SQL；筛选页10为5 HTTP/5 SQL，摘要相同。该证据为现有50股开发池，不是全市场、在线供应商或独立服务迁移验收。

用途反查见 消费项目 InvestMind 仓库的 docs/investmind-lineage.md。

## 股票接口专题

[完整选股条件与排序](screener-api-fields.md) · [银行字段与评分](research/investmind-bank-lineage.md) · [股票工作台与同业研究](workbench-api.md) · [市场总览与浏览](market-supply.md)

[从原字段反查项目、页面、指标](field-consumers.md) · [InvestMind 选股器逐字段用途](screener-field-usage.md) · [证券公告与披露](workbench-announcements.md)

[逐指标留存策略、公式版本和可回溯区间](derived-history.md)
