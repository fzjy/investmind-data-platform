# 公共数据接口目录：股息率试点

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

财务表保留证券、报告期、报告类型、公告日、update_flag 版本唯一键；分红以证券、报告期、公告日、进度区分 Core 版本。原件和账本另处理修订/取消。当前 formula_version 的运行值、Raw 到 Core 所有版本的保留完整性、源公告首次可得时间和可靠历史回溯起点本轮未独立核实；不可宣称无前视历史选股已验收。

来源频率：行情为交易日，财报按公告，分红按方案/实施公告与更正。现登记沿股票同步和发布重算，分红事实变动刷新、未变化默认7日回看；这是已有登记描述。本轮未执行供应商采集，实际调度时刻、任务调用数、全量/增量游标及生产覆盖需运行证据补核。现有入口 backend:update；不得将查询 SQL 数当供应商请求数。

## 数值基线与缺口

`research/investmind-screener-baseline.json`：快照 `20261021-1c01ff6eb0b7d78a`，asOf 2026-10-21。全板块默认50行，三项 null 分别1/8/11；开启 dividendYield>=0 后41行，null 为0/2/5。两组有序行 SHA256 分别 `18760ca76a1daa7d97d7426526f07ee73ca2142d2e50cdd160981631a47bc2fa`、`376995d24708644a3637911519167e783d53466578e7085aa1a3d1d19e970c7e`。默认页200各1 HTTP/1 SQL；筛选页10为5 HTTP/5 SQL，摘要相同。该证据为现有50股开发池，不是全市场、在线供应商或独立服务迁移验收。

用途反查见 消费项目 InvestMind 的 docs/investmind-lineage.md。

