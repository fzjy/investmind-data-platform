# 股票工作台指标、股东与经营面板核验

当前 50 个工作台共用 2026-10-21 行情业务日。下列计数来自逐证券比较。八卡、股东回报、主营分项和经营史使用正式源数据读取与生产公式重放；估值带从 Core 日估值独立计算。每只证券的发布版本、缺项原因、倍数及覆盖见[全池实测结果](investmind-workbench-panels.json)。

## 八张指标卡

`metrics.cards[].id` 将一张卡及其公式、单位、输入来源、披露日期和缺因传给指标速览。实测 400 张卡的重算值均与 `derived.metric_value` 的 v2 同日行相同，卡片详情和格式化显示与快照逐项对应。每只都有正式卡 ID，旧发布的前端回退路径未触发。

| 卡片 ID | 规则与主要输入 | 有值证券数 |
| --- | --- | ---: |
| `workbenchPeTtm` | 未复权收盘 ÷ 连续四个单季基本 EPS 合计。缺合格季度 EPS 或每股口径核验 | 0 |
| `workbenchPb` | 收盘 ÷ 最近报告期每股净资产，需有效 BPS 与每股口径 | 0 |
| `workbenchMarketCap` | 同日收盘 × 总股本。入库单位元，页面换算亿元 | 50 |
| `workbenchNetCash` | 货币资金扣除同一期有息债务，需债务构成及到期项核对 | 0 |
| `workbenchFiscalDividendYield` | 上一财年已付税前每股现金 ÷ 收盘 ×100 | 49 |
| `workbenchFiveYearDividendCount` | 五个已结束财年内，逐笔裁决的独立正现金实付事件数 | 45 |
| `workbenchAnnualDividendYield` | 当年累计利润按历史季节性预测全年，结合股本、加权派息率和收盘 | 42 |
| `workbenchNtmDividendYield` | 逐季预测未来四季利润，结合股本、加权派息率和收盘 | 39 |

贵州茅台市值为 1,637,219,372,013.69 元，FY 股息率为 3.9689720468%，五年实付 9 次，本年预期 4.2884002896%，NTM 预期 3.4588398995%。其报价区有供应商静态 PE 和 PB，指标卡要求独立财报输入；两组字段分别登记。

## 取数与消费

| 发布字段及页面 | 实际来源与已验证内容 | 当前覆盖与保留缺项 |
| --- | --- | --- |
| `quote.industry/subIndustry`，行情标题 | `core.classification`、`core.classification_member` 的来源、层级及有效期，经证券映射规则 → 行业标签 | 两层各 50 有值 |
| `quote.concepts[].name/changePercent`，概念菜单 | 当前工作台组装器返回空数组。行情日内折线消费 `quote.intraday.day/fiveDay`，状态另取 `dataAvailability.intraday` | 50 个概念数组及 100 个分钟序列为空。分钟为当前产品范围选择，概念名称与板块涨跌仍无工作台来源 |
| `financial.dashboard.bands`，仪表盘估值带 | Tushare daily_basic → `core.daily_basic.pe/pb/data.ps_ttm`（PS 缺键时读 data.ps）→ 取三年窗口正值、当前值与均值 ±1/2 倍总体标准差 | 逐点核对 PE 32,929 个、PB/PS 各 36,037 个。茅台当日分别 19.8885、6.5162、9.6221。页面窗口控制还可选择 5 年或 10 年，数据上限取实际供应 |
| `financial.shareholders.annualDistributions`，年度派息与 K 线派息事件 | 原件及 Core 的实付记录、可见年报归母净利 → 财年 DPS、已付金额、支付率与逐笔依据 | 868 个完整财年、49 部分、172 缺失；覆盖完整指截至统计日可核对的实付。2026 尚在年中统计窗口，当前零金额不能解释为全年零派息 |
| `concentrationPeriods`、`periods[].holders`，户均市值与股东构成 | 股东户数原件及 `core.shareholder_count`、同日 `core.daily_basic.total_mv` → 户均市值。Tushare top10_holders 前十大股东 → `core.top_holder` → 最近可见持仓比例、累计、较上期变化 | 1,332 个户数期，1,031 个有户均市值。679 个持仓期，676 个有明细。页面按所选报告期匹配持仓，仅显示最近十个户数期 |
| `financial.dashboard.businessHistory`，产品 / 地域构成 | `fina_mainbz` → 冻结请求及 `raw.source_payload` → 同期同类型、同币种按分项和报告版本选择收入、利润、成本及构成 | 50 只共 1,805 个分组报告，查询结束状态均完整。现有数据只证明报告期间，原始披露日及修订可见性仍缺 |
| `operatingHistory`、`returnHistory`，经营与回报趋势 | 合并财报 revenue、n_income_attr_p、n_cashflow_act、c_pay_acq_const_fiolta → 经营年度及单季记录；`core.financial_indicator` → 可见的报告 ROE、ROIC、两者差值及科目引用 | 921 条经营年度记录、3,363 条经营单季记录，604 条回报报告重放一致；经营趋势不承诺完整历史 |
| `dashboard.peg.value/pe/growth`，PEG | 页面要求结果有限、PE 与增长率均为正 | 当前 0/50 满足，发布器留空 |

## 比值沙盒与用户数据

行情页的收藏按证券将代码、名称保存在网站浏览器本地。加入组合交给网站的组合模块。财报标签、下钻目标、报表期、图表范围、分子分母选择及 DCF 参数是网站交互状态。

比值沙盒先把 `metrics.years/fields` 与已发布三大报表按财年连接，优先用合并年报原数换算亿元，保留旧字段回退和同版冲突的空值。源码注册表再生成可选字段、派生项及预设，用户选择的 1/3/5/10 年窗口在浏览器计算。全池抽验“经营现金流 ÷ 归母净利润”，实际网站计算模块的 1,812 个可用输入与原表逐项相符，50 个最新财年结果均有值。茅台 2025 财年为 615.2220498935 ÷ 823.2006710168 = 0.7473536788 倍。每种任意字段组合、历史多年度窗口、DCF 参数场景未逐一重放。浏览器端核验脚本在 InvestMind 网站仓的 `scripts/audit-workbench-ratio.mjs`，依赖同仓网站组件。

网页独立内容模块还会更新 `quote.mentions/latestMention`。36 只有可见提及，其中 35 只的最近内容晚于价格业务日。按该证券显示的最新提及时间核对来源，再独立统计该时间之后库中已存在的内容；50 只的计数和最新时间全部匹配，未发现之后还有可见关联内容。茅台为 20 次，最近一次是 2026-11-14 01:03:04 UTC。快照缺少显式的“本次内容扫描截止”字段，最后提及时间只证明一条具体内容的公开时间。

在配置只读开发库的仓库根目录运行 `python -I scripts/audit-workbench-panels.py`。底座独立生成数据侧证据；在网站仓执行同一脚本还会加载网站转换器，额外生成 `websiteRatio`。两仓留档包含本批两侧实测结果。[源库核验脚本](../../scripts/audit-workbench-panels.py)
