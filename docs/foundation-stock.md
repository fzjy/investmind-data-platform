# 股票与组合数据合同

股票报价、分红公告、财报版本和交易日历可以按主题读取。组合输入包继续通过已发布版本供 InvestMind 使用。下列公共接口均为本地库存的 GET；查询不启动任务、不消耗供应商限额。通用错误使用平台的 `INVALID_REQUEST`（400）和数据库不可用（503）合同。

## 查询示例

```text
GET /api/v1/datasets/stock-securities?symbol=920047.BJ
GET /api/v1/datasets/stock-trade-calendar?exchange=SSE&start_date=2026-10-01&end_date=2026-10-08
GET /api/v1/datasets/stock-classification-members?symbol=600519.SH&start_date=2026-01-01&end_date=2026-10-21
GET /api/v1/datasets/stock-financial-reports?symbol=600519.SH&statement=income&start_date=2024-12-31&end_date=2025-12-31
GET /api/v1/datasets/stock-disclosures?symbol=600519.SH&start_date=2025-12-31&end_date=2026-06-30
GET /api/v1/datasets/stock-company-events?symbol=600519.SH&kind=repurchase&start_date=2025-01-01&end_date=2026-10-21
GET /api/v1/datasets/stock-dividends?symbol=600519.SH&start_date=2024-01-01&end_date=2026-10-21
GET /api/v1/datasets/stock-dividends?symbol=600519.SH&start_date=2024-01-01&end_date=2026-10-21&as_of=2026-10-21
```

日期使用 `YYYY-MM-DD`，起止均包含当天。每页默认 500 行，最多 2,000 行。续页将 `meta.next_cursor` 原样置于同一筛选条件的 `cursor`，结束时为 null。未知参数、同名多值、反向区间和跨条件游标返回 400。列表每次读取当时的库存，跨请求翻页没有冻结版本；要回放同一数据库状态，可在同一只读事务内使用领域查询。三个日频接口的业务键、数值精度及产品回归数据见[日频合同](stock-datasets.md)。

| 主题 | 行、筛选及唯一排序 | 字段与覆盖 |
| --- | --- | --- |
| 证券主数据 | `symbol` 可选；按市场后缀的完整代码升序 | `core.security` 中原代码、名称、交易所、板块、上市及退市日、状态、ST 标志，附 `security_name_history` 的名称与起止日。北交所 `related_codes` 取当前通过校验的 `bse_mapping` 原件，同一主体的其他代码附 `alias_source_id`；缺件时 `alias_status=unavailable`。沿革表未记录完整历史生效日。 |
| 交易日历 | 单交易所，按 `cal_date` 升序 | 每个已存自然日返回 `is_open` 和 `pretrade_date`。已存休市为 false；库存缺日不生成行。 |
| 分类关系 | 单证券，与查询区间有交集的生效期；按成员行 ID 升序 | 业务键为供应商、来源、分类码、证券、生效日起；`effective_to` 为空表示开放区间。权重采用来源数值与原精度，缺失为 null。 |
| 财务报表 | 单证券，按报告期闭区间；每种报表内按库存行 ID 升序 | `statement` 为 income、balance、cashflow、indicator。报告期、公告日、实际公告日、报表类型、公司类型、修订标志独立输出。保留同一报告期的所有已存版本，以 `source_id` 区分。数值限该表已登记字段，缺字段为 null。金额用供应商元、EPS/BPS 用元/股，比例指标保留供应商口径。产品另依合并/单季、实际披露日和专项原件核对再选版。 |
| 披露日程 | 单证券，按报告期闭区间；按行 ID 升序 | 证券、报告期、公告日、预计日、实际日、修改日。当前表按证券及报告期更新原行，旧披露计划的完整变更时间线不可从本表恢复。 |
| 公司事件 | 单证券、单 `kind`，按公告日闭区间；按主题内行 ID 升序 | forecast 的阶段、利润上下限（万元）及变动幅度（%）；express 的快报金额（元）；repurchase 的流程、截至日期和已记录量额（股、元）；unlock 的流通日与数量（万股）。阶段、报告期、预计截止日和流通日独立成列。缺公告日的行无法归入公告日范围，各版本保留各自的 `source_id`。 |

上述新接口的数量、金额、比率和权重返回十进制**字符串**或 null；财务表及公司事件的 JSON 数字在 PostgreSQL 中按文本提取，避免 Python 浮点反序列化改变有效位。较早的三个日频接口使用 JSON number；希望保留其精度的调用方可用 Decimal 解析响应正文。供应商字段的原始单位和映射见[股票来源目录](data-catalog/stocks.json)。

## 分红的两种问题

**某证券在区间内有哪些公告。** `stock-dividends` 必须指定完整代码和公告日区间。当前库存覆盖 Core 的类型化标准行、已完成股票更新的窄字段响应和绑定完整字段任务的 HTTP 原件。相同事实可以有多个来源行；这一行的唯一标识是 `source_id`。按 `ann_date,source_id` 升序翻页。证券使用请求代码，北交所前后代码可先由主数据查出，再分别查询。缺公告日或公告日不能解析的观测计入该证券的 `missing_announcement_rows`，无法分配到某一公告区间。

**截至某日底座实际捕获了什么。** 传 `as_of` 后只使用按该证券原代码绑定、在上海该日结束前实际捕获、可核对 HTTP 状态、正文哈希、请求哈希、响应栏位和完整分页的专项原件。再排除公告日或实施公告日尚未到达的版本。先前已观察的公告留在记录里，后一次响应省略某行不足以证明已取消。`meta.coverage_scope=captured_as_of`。仅有当前 Core 或后补的原件时，较早时点可返回空。对历史公告的首次公开、公开与捕获间隔内的修订及历史证券代码归并，库存存在缺口。

| 返回项 | 口径 |
| --- | --- |
| 身份与阶段 | `ts_code,end_date,ann_date,div_proc` 保留证券、归属报告期、本版公告日及阶段原文。申请、预案、通过、实施及停止等阶段各自保留。确定同一事件优先用报告期与股权登记日，登记日缺失才使用同一除权日；同报告期可以有多笔派付。 |
| 日期 | `imp_ann_date,record_date,ex_date,pay_date,div_listdate,base_date` 分别表示实施公告、登记、除权、支付、新增股份上市及基准股本日。未公开的实施版受截止日过滤；当时已经公开的未来支付日仍是预定日期。 |
| 现金与股份 | `cash_div_tax` 税前元/股，`cash_div` 供应商税后元/股；`stk_div,stk_bo_rate,stk_co_rate` 总送转/送股/转增比率（股/股），`base_share` 分配基数股本（万股）。零保留为零，缺值为 null。历史 Core 同名 JSON 镜像由类型化列覆盖。 |
| 来源 | `core` 使用标准行，`normalized_capture` 使用窄字段任务验收过的规范响应，`verified_original` 使用绑定的全字段原字节并解析 Decimal。完整原件列示 UTC `captured_at` 和逐行指针。原始正文及凭据内容不经 HTTP 输出。 |
| 修订与冲突 | 字段兼容的窄记录让全字段原件参加裁决，同日同事件重复原件均可作来源。裁决先看证券的可见历史再筛输出区间。已经捕获且符合实施公告时点的无公告日记录同样参加裁决。无法定位或排序的停止记录使同证券同财年的实施版本待核，缺公告日的实施修订使其所属事件待核；可归入查询区间的受影响行返回 `selected_implementation=false`。其分页的 `meta.issues` 用 `sourceIds` 定位问题来源，用 `affectedSourceIds` 标出受影响的已公告行。已选版本的支付是否实际到账仍由使用方按支付日、金额等判断。 |
| 覆盖 | `evidence_status` 为 observed、missing 或 invalid；成功空响应也记录 `captures`。缺件、损坏与成功空结果可区分。`normalized_status` 表达当前标准/窄合同读取是否成功，截点查询为 not_requested。`history_complete` 固定 false。诊断关联当前页行的来源，`captures` 列出时点内验证过的全字段请求（含空响应）。 |

组合的分红账本、股票股息率和这个接口共用已绑定的原件读取及同一套修订、取消与冲突裁决。组合发布另加冻结基版、全历史查询覆盖、已付保护及已付款项更正核验。股票的财年股息率用上市后可验证覆盖的税前每股实付额和当期收盘价；预测与财年聚合继续由产品层处理。公共接口的严格截止日视图与产品按公告日重算且可引用后来补捕的版本，分别回答不同的时间问题。

## 逐主题去向

| 来源目录条目 | 公共查询或内部消费者与处置 |
| --- | --- |
| stock-basic、stock-bse-mapping | 证券主数据、名称沿革及有凭证的北交所关系由 stock-securities 提供。股票快照和组合重建在自己的证券范围上调用 securities_query。 |
| trade-calendar | stock-trade-calendar。选股验证窗口、组合全收益指数核对与冻结合同重放按实际交易所取日历。股票十年补采的专项日历按其原件合同复核。 |
| stock-classification、stock-classification-members | stock-classification-members 提供成员及所属分类。分类树父节点等元数据继续供分类同步和工作台；不把孤立目录节点计作有效成员。 |
| stock-daily、stock-adjustment、stock-daily-basic | 保留三个日频接口。指标卡以每股最近行情日左连同日估值；工作台持股、风险估值、股东回报、市值分母、组合归因和基准起日都复用同一日频范围。停牌核验是以停牌事件为主表的关联，保留专门查询。 |
| stock-income、stock-balance、stock-cashflow、stock-financial-indicator | stock-financial-reports 展示 Core 版本；专项报告字段、披露后选版和同级冲突由 metric_card_originals、metric_report_revisions 按指标需要组合。核心版本与原件仍分别留存。 |
| stock-disclosure | stock-disclosures。财务补采按同一查询的实际披露日选任务。 |
| stock-dividend | stock-dividends。实施阶段及日期校验由股票更新负责，未同步到 Core 的阶段保存在 Raw。当前窄字段现金与专项实施、股本事实分开保留。 |
| stock-forecast、stock-express、stock-repurchase、stock-unlock | stock-company-events 聚合四类结构化公告，各类按自己的字段、单位返回。预告的交易日定位、摘要，回购金额汇总、解禁估算留在工作台。 |
| stock-business | 普通表只有期末、类别、分部与收入；全字段专项包含币种与额外明细。load_business_evidence 先验证请求、类别与原件，再供工作台投影。不同币种或地域层级缺口保留，暂不公开统一分部营收序列。 |
| stock-holder-count、stock-top-holders | 股东户数的 Core 按报告期更新，load_shareholder_counts 合并规范响应恢复修订。前十名持股既有份额又有名称/主体类型，同名异值会阻断占比。分别用于股东回报、持仓结构和风险，未承诺统一的历史身份及股东百分比合同。 |
| stock-ratio-inputs、stock-research、security-search、security-region、stock-announcements | 比率定义、配对研究、搜索排序和地域映射由既有产品组织；可单取的证券资料用主表接口。可展示公告走独立财经内容查询，工作台自定事件位置。 |
| metric-*、card-*、risk-*、screener-*、stock-dividend-yield | 每个原指标、指标卡、风险信号和列表栏继续由公式/版本或快照提供。股票目录逐项列出输入、消费者、单位与版本。长历史 PE/PB 在 risk_valuation_history 通过完整分页后与同日 Core 值核对，冲突字段标缺。计算窗口、预测、评分和展示精度留在产品。 |
| portfolio-inputs | 已发布股票池的证券、价格、复权因子、股本、现金事件、财务及国债与股票/宏观查询共享。全收益基准在冻结合同中验证目标指数、日历及全窗价格；CNY 面值 1、收益模型和缺口标签随组合版本提供。组合 ETag、304 及兼容路由保留原服务入口。 |

## 验收与重跑

本机只读开发库存的选股版为 `20261021-1c01ff6eb0b7d78a`，组合版为 `20261021-690705b397d4b878`，业务日期为 2026-10-21。50 股的 400 个卡片行情/估值输入、150 个已发布股息率、528 行标准分红的 4,224 个字段、50 份组合归因和 50 份组合股息率输入逐项相符。全公告日期窗口有 528 条 Core、2,959 条窄字段观测、5,935 条专项来源行（重复捕获各有来源），50 股均留存了专项凭证。计数单位为观测行，经济事件经过裁决后另计。

组合影子构建还核对了资产列表、复权价、未复权价、因子覆盖、快照日期、分红账本、十年期国债参考和全收益基准，均与已发布值相等。抽取首只样本股的首个捕获日检验日界，前一日无凭证，捕获当日返回的 98 条观察都能追到当天及以前的原件。真实快报样例为 `603019.SH` 的 2026-08-14，解禁样例为 `000333.SZ` 的 2026-07-16。

复核使用 `backend/.venv/Scripts/python.exe -I scripts/verify-foundation-stock.py`。脚本限定本机 PostgreSQL，在同一 REPEATABLE READ、READ ONLY 快照中比较；不会提升发布指针。隔离 PostgreSQL 用例位于 `backend/tests/database_api/test_stock_foundation.py`，包含缺当日估值、只有因子、空股票集合、取消、同报告期多事件、同日异值、关键日缺失、捕获日界线、未来实施公告、成功空响应和损坏原件。对外路由、旧产品发布和采集复用各自验收。历史捕获的逐笔判断及 405 个专项补充事件见[分红采集与存储对账](foundation-audit/dividends.json)。

2026-11-19 隔离 PostgreSQL 验收的三组结果分别为 246、92、21 个用例通过。各组有重叠；后两组覆盖最新的原件匹配、持有同一只读事务和真实发布入口的共享查询调用。各组均报告真实 HTTP 和业务库访问违规为零。

同日补测无公告日的停止和实施修订，173 个分红专项及兼容测试通过。50 股开发库存保留 15 条缺公告日的观测，其中影响已公告实施版本的记录为 0。会引起阻断的情形通过隔离库构造，覆盖实施公告日前后、跨财年、同报告期不同事件及来源分页。开发库存的组合影子构建和股息率再次逐项相符。
