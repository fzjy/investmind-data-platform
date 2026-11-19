# 市场与宏观数据集

市场查询回答某个标的在当地交易日留下了什么事实。标准宏观查询按已保存的发布日期与修订规则回答一个截止时点的问题。专项宏观查询按成功捕获及入库完成时刻重放当时可读的采集窗口。InvestMind 继续在市场概览、探索、宏观传导和洞察的发布过程中组合这些事实。

## 请求合同

以下均为 `GET /api/v1/datasets/` 下的相对路径。区间包含首尾两日，日期使用 `YYYY-MM-DD`，`as_of` 使用带时区的 ISO 时间。单序列结果按业务日期升序，区间查询的 `limit` 默认为 500，最大 2,000。`data` 是记录列表，`meta.next_cursor` 用于下一页，末页为 null。游标绑定数据集和除页大小以外的全部条件。跨查询游标、未知参数、重复参数、倒序日期在正式应用中返回 400。空集合返回 200。数值用 JSON number 传输并保留数据库十进制精度。

| 路径 | 必填筛选与业务键 | 内容和单位 |
| --- | --- | --- |
| `market-daily` | `asset_type=indices\|etfs`、`asset_code`、`start_date`、`end_date`。键为供应商、类别、代码、当地交易日 | 读取 Tushare 当前标准库的 OHLC、昨收、百分数涨跌幅。指数单位为点，本合同的境内 ETF 单位为人民币元。保留 API 名、原件 ID 和捕获时刻。行情允许后续更正，单个业务键保存当前行。 |
| `etf-trading` | `asset_code`、`trade_date`。键为基金、交易日 | 专项成交与份额分开验原件、批次及成功游标。每部分给出其原件 ID、捕获时刻。成交量为手、金额为千元、份额为万份。仅一项存在时另一项为 null。两项均缺时返回空集合。 |
| `commodity-daily` | `asset_code`、`window_end`、`start_date`、`end_date`。键为已存采集窗口、主力品种、交易日 | 四种原件共同核实开市日、当日主力月合约、上市摘牌期、合约乘数及报价单位。公开未复权 OHLC、昨收、昨结、结算价、量、额及持仓。量与持仓为手，金额为万元，价格单位随合约。每行列出四份来源 ID 和捕获时刻。任一必要分量缺失时该品种窗口返回空。其他品种可独立查询。 |
| `stock-limit-status` | `symbol`、`trade_date`。键为股票、交易日 | 读取 `daily_basic.data.limit_status`。专项标记须匹配 Tushare 原件、请求、批次和成功游标。旧行仅有标准 JSON 字段时标为 `core:daily_basic.data`，原件 ID 为 null。保留 0 至 6 的真实码。缺值或专项凭证损坏时返回空。 |
| `macro-observations` | `series_id`、`start_date`、`end_date`、`as_of`。键为系列、观测日期，版本身份含来源、发布时间与修订标识 | 20 个标准系列返回原单位、季调口径、频率、值、观测日、发布时间、修订标识与来源链接。按 UTC 截止，先按同源选择最新已披露版，再排除已到期版本，最后按目录优先级或核实的文件导入选源。到期边界为左闭右开，修订到期不会复活同源旧版。 |
| `macro-specialty-observations` | 同上。系列为 `cn.gdp.nominal_cumulative` 或 `fx.usdcnh` | 按截止时间选该组最近完成且已通过作业、原件及 Core 绑定核验的窗口。返回当年累计名义 GDP（亿元，季度首日定位）或离岸汇价（CNH/USD，保留报价侧），以及窗口指纹、检索时间和原件中的行号。采集完成之后才看得到整个回溯窗口。 |

`meta.coverage_scope` 在前四项为 `stored`，标准宏观为 `release_as_of`，宏观专题为 `capture_as_of`。标准系列依库存记载的观测期和发布时间重述历史。该系列没有逐行本地首次入库时间，严格按终端在历史当日收到的数据重放需要额外凭证。

**历史采集合同。** ETF 按基金及交易日定位原请求，商品从该品种、窗口末日的主力映射原件取回窗口起点。新捕获将合同指纹纳入 Raw 正文哈希，旧捕获使用作业业务键保留的指纹。每个候选均须重新得到原成功游标键，并通过原件哈希、请求参数、采集批次、捕获时刻和归一化校验。旧直写若同时缺少作业和指纹，只能在已有目录资产定义与原游标仍可匹配时恢复，商品窗长取自原件。同日其他标的已有作业时，两类候选均验原游标。当前目录无法读取时，已有作业登记的历史合同仍可验证。正式产品发布继续按当前目录验证目标。

同一基金、同日有多个合同时，ETF 从最后捕获的有效合同中返回零至两个分量。商品始终在同一个指纹和起止日期中核验完整的四类分量。若同一窗口末日有多个已验证窗口，优先选择覆盖请求起点的窗口。若均无法覆盖，则优先覆盖更多天数。覆盖相当时选构成原件最后捕获时间较晚的窗口。并列时可取完整包含其他候选相同事实的一组，无法如此判定时返回 502 `INVALID_SOURCE_PAYLOAD`。

```text
GET /api/v1/datasets/market-daily?asset_type=indices&asset_code=SPX&start_date=2026-10-12&end_date=2026-10-16
GET /api/v1/datasets/etf-trading?asset_code=510300.SH&trade_date=2026-10-16
GET /api/v1/datasets/commodity-daily?asset_code=AU.SHF&window_end=2026-10-16&start_date=2026-10-12&end_date=2026-10-16
GET /api/v1/datasets/stock-limit-status?symbol=600519.SH&trade_date=2026-10-16
GET /api/v1/datasets/macro-observations?series_id=us.labor.nonfarm_payroll_change&start_date=2026-07-01&end_date=2026-07-01&as_of=2026-10-02T00:00:00Z
GET /api/v1/datasets/macro-specialty-observations?series_id=cn.gdp.nominal_cumulative&start_date=2025-01-01&end_date=2026-09-30&as_of=2026-11-08T14:51:37Z
```

## 市场目录 17 项的消费位置

| 目录 ID | 适用范围及决定 |
| --- | --- |
| `index-daily`、`index-global`、`etf-daily` | `market-daily` 按类别与代码读取。市场概览的 6 条指数、9 条 ETF 在成对发布事务使用同一日线选择器，并验证完整目标窗口的 Raw 血缘。当地交易日分别保留，日历状态未知时保持 unknown。ETF 专项量额另见 `etf-trading`。 |
| `etf-shares` | `etf-trading.shares`。热力图还核验当日收盘及涨幅与标准日线相符，九格完整时按份额与收盘价计算规模。与标准行情同名的 `fund_daily` 捕获有不同字段与请求范围，保留两类凭证。 |
| `commodity-contracts`、`commodity-mapping`、`commodity-calendar`、`commodity-daily` | `commodity-daily` 每行给出有效月合约、单位、开市日行情和映射依据。采集合同围绕主力窗口，未覆盖合约全生命周期、所有月份及全球交易所日历，相关原件保留。商品页以相同逐日事实聚合周月 K，保留真实换月标识。 |
| `market-daily-summary`、`market-margin`、`market-sz-summary` | `daily_info`、`margin`、`sz_daily_info` 分别有 4、6、1 份 Raw，专项冻结计划带个人本地非商业用途范围。宏观专项用同日、同板块口径计算融资余额与流通市值比。深市校准样本单独保留。 |
| `fx-daily` | 离岸 USD/CNH 在 `macro-specialty-observations` 中按报价侧和捕获窗口查询。标准在岸 USD/CNY 使用各自系列。开发库保存的离岸窗口为空。 |
| `index-basic`、`etf-basic`、`index-weights` | 分别有 2、0、0 份同名供应商 Raw。企业样本按冻结目标和文件判定指数身份、ETF 跟踪关系及成员范围。目录中的局部凭证尚不能还原全市场成分沿革。官方成员文件及样本交集继续随宏观企业分析提供。 |
| `market-overview`、`market-explorer` | 保留已发布产品。涨跌榜、宽度、成交排名和行业热力图读当日股票池，地域来自核验后的证券资料，缺状态的股票不作为正常交易计数。ETF 与商品各用自己的行情日。探索页 `news=latest` 从新闻接口读取当前可展示页。`news=snapshot` 读取该发布版本的内容。 |

## 宏观目录 39 项的消费位置

20 个标准系列均通过 `macro-observations` 独立查询。下表列出冻结发布时点与独立 SQL、宏观洞察沙盘同时吻合的逐系列行数。中国各项及美国各项分别沿用现有目录定义的来源优先级。

| 系列 | 行数 | 系列 | 行数 |
| --- | ---: | --- | ---: |
| `cn.gdp.real_yoy` | 40 | `cn.cpi.yoy` | 120 |
| `cn.cpi.core_yoy` | 121 | `cn.ppi.yoy` | 120 |
| `cn.pmi.manufacturing` | 120 | `cn.industry.value_added_yoy` | 101 |
| `cn.money.m1_yoy` | 120 | `cn.money.m2_yoy` | 120 |
| `cn.credit.tsf_stock_yoy` | 0 | `cn.rate.dr007` | 2,504 |
| `cn.rate.govbond_10y` | 2,504 | `cn.fx.usdcny` | 0 |
| `us.gdp.real_qoq_annualized` | 39 | `us.cpi.yoy` | 119 |
| `us.cpi.core_yoy` | 119 | `us.pce.core_yoy` | 119 |
| `us.labor.unemployment_rate` | 119 | `us.labor.nonfarm_payroll_change` | 120 |
| `us.rate.effective_fed_funds` | 3,673 | `us.rate.treasury_10y` | 2,512 |

| 其余目录 ID | 消费位置与保留原因 |
| --- | --- |
| `source-cn_gdp` | 同一供应商接口的实际 GDP 增长率和累计名义金额有各自的单位和可见时间。原字段（含伴随诊断增速）作为来源凭证保留，已归一的事实分别走两个宏观公共查询。 |
| `macro-extension-usdcnh`、`macro-extension-nominalGdp` | 专项公共查询与发布器共享已完成采集的证据读取。发布版分别有 0 和 20 点。名义 GDP 的单季、同比、平减近似及严格公式继续由洞察按季度对齐。 |
| `macro-extension-vix` | FRED VIXCLS 该版本 129 点，带 CBOE 署名与个人本地非商业用途限定，继续通过保留该归属及用途说明的专题提供。 |
| `macro-extension-governmentBonds` | 社融增量表在 48 月发布窗口仅 3 个可用月，按年度文档来源、检索时间和最近一次失败展示。窗口依采集时点回溯发布，原件和缺期由财政视图留存。 |
| `macro-extension-householdCredit` | 人民银行只存两份对应期间的累计值。单月派生依年内邻期差分。当前窗口仅一个结果。官方专题一并提供原件、累计和差分。 |
| `macro-extension-tier1Housing` | 一线新房、二手房为两条系列，各有 42 点。专项按冻结目录、官方报告、历史证据组及归一化完成时间重述。现有官方专题保留两种市场、证据及缺期。 |
| `macro-extension-specialBonds` | 预算、发行和发布会的累计额与单月直报有跨版差异。专项保留不同文档、原报告进度和差分缺口，避免建立无来源优先级的单值历史。 |
| `macro-extension-propertyLoans` | 房地产贷款余额与同比的 16 个季度均缺，保留官方报告任务、失败状态与空系列。 |
| `macro-extension-marginBalances`、`macro-extension-aShareFloatMv` | 同日融资余额和沪深 A 股流通市值在该窗口仅一个可合成月。交易所样本合同限定个人本地非商业，专项分别保留北交所值、沪深同日证据和单位校准。 |
| `macro-enterprise` | 同期财报可见性、样本池、行业/官方成员交集、覆盖门槛及输入哈希决定结果。保留传导页及其按版企业范围读取。 |
| `macro-spreads`、`cn.ppi_cpi_spread`、`cn.m2_m1_spread`、`cn.money.m1_minus_m2_yoy`、`cn.govbond_dr007_spread`、`us.treasury_fed_funds_spread`、`cn.us.rate_spread` | 传导及洞察同版从标准系列生成。相减顺序、观测期对齐、频率及过期规则均影响结果，保留各自公式、单位和缺口状态。公共调用方可独立查询任一输入。 |

## 对账、发布与回退

开发库存通过 `open_readonly_factory` 在同一 PostgreSQL 可重复读事务中核对实际 GET 和独立 SQL。标准行情逐字段核对 15 个标的共 114 条，9 只 ETF 的原件引用、量、额及份额与探索页吻合，10 个主力品种各 79 根日 K 与发布页逐字段吻合。36,183 个非空原始状态中有 50 个专项标记，本次按状态与证据种类选取 10 行比较。旧发布当日状态覆盖可与其他库存日不同。[逐项回执](research/foundation-markets-macro.json)

宏观 `20261108-366f83bfc727036c` 在 `2026-11-08T14:51:37.835243Z` 的 12,690 个标准观测全部与独立 SQL 和洞察沙盘一致，另有 20 个累计名义 GDP 专项观测。库存中 553 个同系列、同来源、同观测期组同时存有不同发布时间和值。真实回放选取美国 2026 年 7 月非农就业变化。在 `2026-10-01T23:59:59.999999Z` 读到 FRED 9 月 4 日版的 21，在 `2026-10-02T00:00:00Z` 读到 10 月 2 日版的 −10，单位为千人。采集时间、来源发布日期、观测日期各自保留。[复核程序](../scripts/audit-market-macro-datasets.py)

同一只读事务也运行实际构建器，ETF 九格逐字段匹配探索页，宏观传导的 `transmission`、`analysis` 及洞察的 `sandboxIndicators`、`trendDefinitions`、`sandboxMonths` 均匹配现有版本。当前商品构建器与已发布旧包间存在两项可复算差异。10 个品种的旧涨幅分母为昨结算，当前分母为上一交易日主力序列的收盘价。旧包还列有“分时”选项，各品种分时均为空，当前选项为日、周、月。两方涨幅各按所属发布合同验证，其余字段逐项相等。两个空标准宏观系列的目录 `sources=[]`，复核时整张 Core 表中也各为 0 条。[对账明细](research/foundation-markets-macro.json)

每个指数/ETF 区间 GET 按业务键过滤、按交易日取下一页，只需 1 次 SQL。宏观单页先圈定有限的不同观测日期，再读取这些日期的完整候选版本，以免切断修订。不合格或已失效的日期会继续向后查。导入台账在 SQL 中按系列、来源和候选观测期筛选，保留旧版蛇形字段和非规范日期交给原校验器复核。隔离用例中 21 条已导入记录仅有 1 条进入 Python 凭证校验。查询不依赖新建索引。商品窗口按品种只验一次各合约原件。周月派生由产品完成。只读回放同时调整了全部名称、资产顺序和商品窗长，9 只 ETF、790 条商品日线及来源引用逐字段一致。[逐查询成本](research/foundation-markets-macro.json)

发布仍使用原有市场双指针和宏观同版发布机制。隔离库用例覆盖缺一标的阻断推广、第二次写入失败时整对回滚、补齐后重试、ETF/商品跨日携带旧包以及宏观按历史版读取。实时公共入口可按域撤回路由，产品读取可回到先前代码和已有快照，底层表、原件、旧版本都可继续用于复算。市场快照默认新闻与请求时新闻另按各自时钟核对。[市场历史重放](market-history.md) · [市场消费合同](market-supply.md) · [宏观供给](macro-supply.md) · [专题原件重算](macro-specialties.md)

2026-11-19 的隔离 PostgreSQL 17 回归分组记录如下，各组存在交叉用例。宏观生产构建、受控导入、同步及修订 73 项通过。ETF 跨日回退、公共接口及模块边界 25 项通过。市场报价凭证、缺少迁移时的错误合同、成对发布和六个查询入口的终验 33 项通过。三组均记录真实 HTTP 和业务数据库访问违规为 0。

冻结合同修复后，ETF、商品及宏观导入回归组通过 125 项。包含两个完整商品窗口、并列冲突、目录变更、损坏指纹和旧式直接落库的终验组通过 18 项。两次均在一次性 PostgreSQL 17 完成，项目隔离器记录业务库和真实 HTTP 访问违规为 0。

同日其他标的已经登记作业、旧标的由直写入库的混合场景，分别用两个 ETF 和黄金、白银的不同窗口复现。补充用例与 ETF、商品读取、数据集 HTTP 和模块边界共 72 项在隔离 PostgreSQL 通过。开发库只读对账在目录变更后仍重现全部 9 只 ETF 和 790 根商品日线。

```powershell
backend/.venv/Scripts/python.exe -I scripts/audit-market-macro-datasets.py
backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py --postgres pytest backend/tests/market_data/test_foundation.py backend/tests/market_data/test_frozen_contracts.py backend/tests/macro/test_foundation_observations.py -q --tb=short
```
