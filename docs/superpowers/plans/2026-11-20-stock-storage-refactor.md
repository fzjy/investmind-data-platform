# 数据底座股票存储重构实施计划

> 后续获准实施时，使用 `superpowers:executing-plans` 或 `superpowers:subagent-driven-development` 逐项执行。复选框表示实施状态。

**目标**　用标准事实和现行公式生成并保留最新个股及选股发布结果，退役已无需要的历史正文副本和逐版本图点，保留现有功能、证据和数据口径。

**架构**　Core 供给业务计算所用的基础事实。Derived 保留符合现行口径的结果和输入引用。股票发布器校验后更新最新快照，股票接口沿用快照读取。Raw 负责采集和溯源，Ops 负责执行和质量。五个公共数据包沿用现有发布和读取合同。

**技术栈**　沿用 Python、FastAPI、SQLAlchemy、Alembic 和 PostgreSQL。使用项目现有的隔离 PostgreSQL 回归、只读审计和备份恢复工具。

**范围依据**　以本计划「范围与约束」中的会话决定为准。现状依据为[五域数据与消费者清单](../../foundation-audit/inventory.md)、[日频存储对账](../../stock-datasets.md)、[派生结果留存](../../derived-history.md)及[快照存储研究](../../research/investmind-storage-review.md)。[前一轮实施和验收记录](2026-11-19-data-foundation-refactor.md)用于复用已有证据。

## 范围与约束

- **每股保留一份最新工作台快照。** 正文和发布元数据保存在 `mart.published_snapshot`，新版本校验通过后替换同一行。取消个股和选股的在线按需组装。选股沿用现有发布读取，按先前确认的方向仅保留最新批次及其读取所需记录。现有 API 路径、业务字段、单位、缺项、筛选排序和分页语义作为验收基准。
- **五个公共数据包沿用现行方式。** `market-overview`、`market-explorer`、`macro-transmission`、`macro-insights`、`portfolio` 的当前和历史正文、股票池依赖、发布和读取路径按现状验收。保留 `mart.published_snapshot`、`mart.page_snapshot` 及五个包引用的数据。
- **股票业务计算从 Core 取基础事实。** 先补齐个股、选股和取证所需的专项输入。逐项验证 Raw 读取已退出这些计算路径。五个公共包中仍由专项原件直接供数的路径登记待后续迁移，原有运行继续使用其既有输入。
- **将 `core.daily_price.data` 和 `core.daily_basic.data` 中的字段全部映射到具名列，再删除这两个 `data` 列。** 已有对应列的字段使用已有列。新增列保留业务含义、单位、精度、零和缺失语义。涉及 `data` 的整行签名、血缘和覆盖判断也需纳入。
- **每个指标只使用一套现行公式。** 已知错误或无法确认符合现行公式及输入口径的历史结果不得供新路径复用。业务含义不同的指标仍按各自 ID 管理。保留计算所用原料日期和实际计算时间。公式修正的结果重算与追溯方案写进任务 4。
- **时光机以 Core 已保留的历史事实为输入，并可复用匹配的 Derived 结果。** 本计划交付可按截止日读取已知输入的能力和覆盖回执。Core 全量追加修订、每张表统一添加三个时间字段、派生结果保留天数、时光机页面均待另行定义。遇到同键覆盖后的历史缺口，应明确返回缺口。
- **桌面研究证据采用当时使用的 Core 数据、来源和 Derived 计算过程。** 底座交付证据合同。桌面项目负责本地保存历史任务的页面数据、视图状态和证据，具体实现另立桌面任务。清理旧版股票快照前，须联测旧任务本地回看和新版任务取证。
- **Raw 正文清理、Ops 已投递新闻瘦身及 Raw → Ops 外键调整按证据决定。** 本轮交付可执行的留存处置清单。未定保留期限的记录继续以现有策略管理。引擎选型待减容和性能测量完成后再评估。

## 实施前后的核对重点

| 核对场景 | 期望行为 | 验收任务 |
|---|---|---|
| 同一报告期有异值、分红同一期多事件或后续取消 | 按已有业务口径选事实，冲突和覆盖缺口可追查 | 任务 2 |
| JSON 中缺键、显式 `null` 、零值、十进制精度和独有扩展字段 | 各值的含义、结果和来源证明与现有合同一致 | 任务 3 |
| Core 更新完成一半、Derived 仍是上一批 | 最新快照仍由上一批成功发布提供，候选通过现有检查后才切换 | 任务 4 |
| 翻页过程中更新或任务切换股票池 | 维持选股当前发布和前端同版检查，清理旧版时保住当前批次 | 任务 7 |
| 已开始的研究遇到新版，或恢复旧任务时底座离线 | 本轮证据只引用实际采用的值，已有本地证据和工作区可离线打开 | 任务 6 |
| 旧备份恢复、候选切回、股票表退役后公共包读取 | 五个公共包的历史和条件请求可用，恢复后的容量和股票结果可核对 | 任务 7 |

## 文件与职责

| 位置 | 计划承担的改动 |
|---|---|
| `backend/app/db/models/core.py`、`backend/migrations/versions/` | 按实际业务粒度补 Core 字段或事实表，兼容回填，最后收紧结构。 |
| `backend/app/ingestion/`、`backend/app/stocks/dataset_query.py`、`backend/app/stocks/derived/inputs.py`、各股票专项读取模块 | 采集后归一化，统一领域取数，复用已验过的选版及冲突规则。 |
| `backend/app/stocks/derived/registry.py`、`backend/app/stocks/derived/engine.py`、`backend/app/stocks/derived/workbench_cards.py` | 核对现行指标定义、重算结果及其输入引用。 |
| `backend/app/stocks/update/publication.py`、`backend/app/stocks/marts/`、`backend/app/marts/publication.py` | 用已校验的 Core 和 Derived 结果生成最新版，关闭历史工作台副本和逐版本图点写入。 |
| `backend/app/stocks/database_provider.py`、`backend/app/bootstrap.py`、`backend/app/acquisition/availability/published.py` | 以最新版工作台作为当前读取依据，迁出对退役历史表的回退、启动和状态查询依赖。 |
| `backend/app/finance_mcp/tools.py`、`backend/app/finance_mcp/evidence.py` | 将研究证据绑定到本轮实际采用的 Core 值及 Derived 计算过程。 |
| `backend/app/ops/backup/inventory.py`、`backend/app/ops/backup/api_probe.py`、现有审计和回归脚本 | 更新受支持的库存清单和恢复探针。检查直接 SQL、外键、新闻留存和管理台的依赖。 |

## 任务 1　固定基线和字段清单

**输出**　可复跑的股票与现有消费者基线。以本机审计作参考，实施开始时重新统计当前库存。

- [ ] 在同一只读事务内记录迁移版本、全库与各表含索引体积、股票与公共包的业务日、发布编号和接口响应摘要。优先复用 `scripts/audit-platform-storage.py`、`scripts/verify-foundation-stock.py`、`scripts/verify-investmind-connection.py`。按同一压缩和恢复口径记录备份大小。保留对照时使用的配置、时点和样本范围。
- [ ] 从 141 项既有目录出发，逐项写明股票功能实际读的字段、业务键、日期含义、单位、所需窗口、当前来源、写入任务和消费者。标出 Raw 独有字段或日期、Core 已有但取数旁路、真正未采到的数据，以及只属于五个公共包的待迁移依赖。
- [ ] 固定工作台和选股回归样本，包括普通、银行、缺财报、缺长估值窗口的股票。固定新闻与公告查询时点，登记三种股息率、风险卡、筛选、排序和分页。对五个公共包另保存相同输入下的对照结果，包含组合 ETag 和 `304`。
- [ ] 在指标、证券池和数据量均相同的样本上记录现有快照读取的响应时延、SQL 次数和返回体积。目标部署规模已有明确计划时，再使用现有 `capacity-benchmark` 扩大数据库样本，另测股票后台构建和最新快照读取。压测结果注明合成行情覆盖范围和真实业务输入缺口。

**验收**　每个股票字段能定位到消费者和对应数据源。所有现有工作台、选股及五个公共包读取入口均有基线。记录发现的现状差异，后续对照逐项解释。

## 任务 2　补齐股票计算所用的 Core 事实

**文件**　`core.py`、对应 Alembic 迁移、各专项读取模块、`dataset_query.py`、`derived/inputs.py`、`backend/tests/database_api/test_stock_foundation.py`。

**接口**　保持既有 `daily_rows_query`、财务事实查询和 `dividend_observations` 的调用语义。面向新路径的查询接收调用方的数据库事务、证券范围和业务时点。已归一值返回可追溯的 Core 记录标识。

- [ ] 为长窗口 PE/PB、专项报表、分红实施与送转、股本、主营分部、股东户数及持股等逐项写出目标字段、业务键和来源优先规则。优先补同业务粒度的现有表。存在独立来源、事件或口径时，仅在现有键无法表达且实际消费者需要时增加事实表或键。
- [ ] 先加兼容结构，再从已验证的采集凭证回填。按任务 1 的数据范围和原件行定位追踪回填。把解析、单位换算和质量校验留在采集或迁移路径。回填重复运行时新增和变更都为零。已有冲突和覆盖空缺需能逐键核验。
- [ ] 新采集在完成相同校验后写入标准事实。将个股、选股、风险和证据所用的股票查询逐项切到 Core。新增派生或业务请求不得为读取计算值去解析 Raw 正文。专项取证和离线审计仍可通过来源引用核验材料。
- [ ] 只补实际有依据的时间字段或凭证关联。验证交易日、报告期、已知公开时间、捕获时间分别可解释。保留现有已登记修订的读取。对无法从现有 Core 重建的时点，输出缺哪个事实或时间依据。
- [ ] 在 `test_stock_foundation.py` 增加回归，覆盖长估值仅专项有值、同日冲突、分红取消及同报告期多事件、报表公告恰在截止日边界和重复回填。再对新路径禁止读取 Raw 正文，断言参考工作台和选股的关键字段与已校验基线一致。

**验收**　可比业务键、数值、空值和单位逐字段对账。已补齐输入直接从 Core 使用。真正历史缺口有清单和接口表现。

## 任务 3　将日频 JSON 字段迁入普通列

**文件**　`daily_price`、`daily_basic` 对应模型与写入器，`backend/app/stocks/update/publication.py`、`backend/app/stocks/datasets_routes.py`、`backend/app/portfolio/attribution_inputs.py`、日频与股票更新回归。

- [ ] 对两张表的 `data` 列导出逐键清单，记录同名普通列、相等数、单边为空数、异值数、类型和当前读写路径。覆盖现有库存和采集入口可能写入的字段。已读取的 36,989 行 `daily_price.data` 共 11 个键，均有对应列。`daily_basic.data` 共 17 个顶层键，10 个有对应列。其余是 `pe_ttm`、`ps_ttm`、`total_share`、`float_share`、`free_share`、`limit_status` 和 `_limitStatusLineage`，其中嵌套对象包含 `sourcePayloadId`、`contentHash`、`fingerprint`。以这份本机盘点作为迁移前复核的起点。
- [ ] 建立逐字段映射。同义字段沿用现有列。为 `pe_ttm`、`ps_ttm`、三个股本数量和 `limit_status` 增加具名列。把状态来源对象拆成 `limit_status_source_payload_id`、`limit_status_content_hash`、`limit_status_fingerprint`。字段类型按源值精度、长度和已有接口合同确定。历史数据出现未登记键时，先登记映射或查明含义，再执行删列迁移。
- [ ] 先按原精度回填，切换读写与摘要生成。覆盖 `data` 访问中的 ORM、原生 SQL、整行指纹及市场涨跌停证明。异值按已验证来源处理。业务依赖显式空值和缺键之分的地方要保留其状态语义。历史回填重复运行收敛为零。
- [ ] 验证 `0`、显式 `null`、缺键、边界小数和异值样本，逐项对账股本归因、每日交易与估值、涨跌停状态及日线接口。确认所有读写方已迁走，且隔离旧库的升级和恢复通过后，删除两个 `data` 列。
- [ ] 在隔离副本以相同口径记录变更前后的列大小、关系大小和备份大小，区别逻辑删减与磁盘实际回收。

**验收**　每个 JSON 键都有明确的具名列。两个 `data` 列退役后，日频、估值及现有消费者通过回归，值和来源能够追溯。

## 任务 4　统一现行公式与最新股票发布校验

**文件**　`derived/registry.py`、`derived/engine.py`、`derived/workbench_cards.py`、`stocks/update/publication.py`，以及质量检查和更新回归。

**接口**　沿用现有股票更新和发布合同，按股票池、业务日和已核验的输入校对本轮指标及候选快照。

- [ ] 为已消费的每个指标确认一套现行定义，记录单位、输入、回看窗口、缺值及适用条件。比如工作台股息率和筛选分位要逐项对照，避免把相同显示名但含义不同的值合并。以同一批历史输入检验现行定义，列出有依据的口径差异。
- [ ] 新路径只用现行定义。将已确认使用错误公式的结果标为不可用，用已核验的 Core 输入重算受影响的记录。只有能核对公式、输入口径和数据批次均符合现行要求的结果才可复用。无法重算时返回缺项及原因。回归覆盖错误结果碰巧与正确值相同和版本名相同但输入不符的情况。旧结果的物理清理随恢复验证执行，旧研究材料以已保存档案核对。
- [ ] 复用现有发布质量门禁和原子切换，验证候选正文使用本次核对过的输入及现行公式结果。校验失败时继续提供上一份成功发布的快照。
- [ ] 在隔离 PostgreSQL 测试并发读、半途失败、同日变更、重试及过时发布。断言读取者看到完整旧版或完整新版，失败的候选无法覆盖最新版。历史日原料不足时给出实际缺项。

**验收**　当前指标口径一致，只有核验过的派生结果参与最新版发布，快照继续提供既有的股票池、业务日和数据版本。

## 任务 5　已取消的按需组装方案

经确认取消个股和选股的在线按需组装及其关联的股票池读取替换。以下保留已完成的只读探测结果，供将来重新评估时参考。

**实施前只读查询计数**　使用本机 PostgreSQL 的可重复读只读事务，业务日为 2026-10-21。复用现有仓库、组装器和股票发布前的指标卡、风险、股东回报处理，只统计业务 SQL，连接初始化、HTTP、采集和发布均不在计量内。个股测茅台主体，未另组装完整的侧边股票池。选股测现有 50 股全集，筛选、排序和分页本身在内存中完成。

| 路径 | 业务 SQL 数 | 单次探测耗时 |
|---|---:|---:|
| 当前快照读取，茅台工作台 | 1 | 0.035 秒 |
| 当前快照读取，选股 | 1 | 0.025 秒 |
| 复用现有组装链，茅台工作台 | **3,030**（输入 49、准备 1,425、Derived 2、正文组装 0、补充卡片/风险/回报 1,554） | 约 17 秒 |
| 复用现有组装链，50 股选股 | **12,750**（输入 886、组装 11,864） | 约 76 秒 |

这些数字衡量现有后台组装链搬到请求路径的成本。SQL 主要集中在反复查询专项的 `ops.job_cursor`、`raw.source_payload` 和 `raw.ingestion_batch`。实际发布仍从 Core 和 Derived 构建最新版，网页请求仍读取已发布快照。

## 任务 6　改接研究证据并完成桌面联测

**文件**　`backend/app/finance_mcp/tools.py`、`backend/app/finance_mcp/evidence.py`、`backend/tests/test_finance_evidence.py`、`backend/tests/test_finance_mcp.py`。桌面端另交本地工作区归档和恢复实现。

- [ ] 从当前已发布工作台及其绑定的指标输入、计算记录取得本轮获准展示的证据材料，沿用证券、指标和研究上下文的绑定校验。证据包括实际采用的 Core 值、单位、日期、来源和记录标识，以及 Derived 的结果、计算过程和缺项。研究取证脱离 `read_versioned(stock-workbench, …)` 后仍能使用同一轮输入。
- [ ] 实施建议是在 `get_stock_workbench` 的选定指标请求中，一次返回同一读取批次的工作台及对应计算依据，由桌面 Host 在提交本轮研究前保存。服务端校验请求的证券、批次和指标。如果工作区加载与研究开始间已换批，告知调用方刷新或重新发起，避免用当前值冒充旧批。测试两次读取之间发布切换或 Core 值更新时，已保存的本轮材料仍和当时的返回一致。保留错证券、错版本、错指标、越权范围和缺失输入的处理。
- [ ] 与桌面项目联测新轮归档实际页面数据、证券和界面状态以及证据。断开底座并重开旧任务，核对工作区显示本轮数据、来源和计算过程。既有只有证据或视图状态的旧任务标明实际可恢复的范围。新任务取当前数据，旧任务继续读取本地档案。

**验收**　正常查看计算依据无需 Raw 正文和服务器上的历史股票页面。实际研究材料可在本地离线对照。桌面联测未完成前保留用于兼容现有客户端的股票历史读取路径。

## 任务 7　保留最新版，退役重复股票快照

**文件**　`marts/publication.py`、`db/models/mart.py`、后续 Alembic 迁移、`ops/backup/inventory.py`、`ops/backup/api_probe.py`、新闻保留过程、恢复和发布回归。

- [ ] 创建完整备份并在隔离 PostgreSQL 恢复。记录股票和五个公共包的接口对照、版本、校验规则和可回退的程序及数据库版本。演练在切换后已有新数据的情况下如何恢复旧读取或重建旧格式当前结果。
- [ ] 在隔离环境验证每只股票从 `mart.published_snapshot` 取最新工作台正文，新版本通过校验后更新同一行。当前工作台记录缺失时沿用 `DATA_NOT_READY` 行为。迁走对 `mart.stock_workbench_snapshot` 的启动、回退、状态以及新闻留存依赖。停止写该表、`mart.stock_daily_chart` 以及 `mart.page_snapshot` 中的个股历史。继续在构建最新快照时由行情和因子生成 K 线并组装到正文。
- [ ] 形成物理清理清单并验收获批范围。退役 `mart.stock_daily_chart` 和 `mart.stock_workbench_snapshot`，清理 `mart.page_snapshot` 的工作台历史正文。选股在新批次成功切换后清理被替换的旧版，保住 `mart.published_snapshot` 中的当前选股记录、其引用的 `mart.page_snapshot` 正文，以及 `mart.stock_screener_daily` 中的同批回退行。五个公共包及其引用的历史行逐项校验。检查外键引用和桌面取证联测后，再清理已有旧版。切换、停写和物理清理分开执行。
- [ ] 核对常规采集、公告修订、风险投影、补采、启动、数据状态、新闻留存和恢复探针。验证正常更新、失败重试和同日重发时，每股仍只有一份当前工作台正文。分别记录使用中的关系、隔离恢复后的数据库和备份体积，说明历史正文和独立图点的实际变化。

**验收**　清理重复股票快照后，工作台与选股仍从最新版读取，桌面取证和恢复探针可用。五个公共包的接口、版本和鲜度可对照。整库隔离恢复通过，并有记录的回退结果。

## 任务 8　提交 Raw 与 Ops 留存处置清单

**文件**　`backend/app/content/news_storage.py`、`backend/app/content/news_retention.py`、`backend/app/db/models/raw.py`、`backend/app/db/models/news.py`、企业专项模型、Raw 与备份的审计脚本。

- [ ] 重新扫描 Raw 正文的消费者，按数据集写出仍需完整正文或原字节的用途、最小来源凭证、外键、已验证的保留边界及删除后的功能影响。单独列明五个公共包仍用的专项材料。对同请求不同正文保留差异证据。
- [ ] 对已投递新闻验证只留来源、上游 ID、内容哈希、抓取和确认状态时的去重、修订、崩溃恢复和留存表现。对照 Ops 队列、Raw 导入记录、Core 可展示新闻的实际职责和记录数。输出可清理的 JSON 范围及预计收益，待用途和期限确定后实施。
- [ ] 对 Raw → Ops 的专项外键检查留存期和引用闭包。需要独立清理 Ops 执行明细时，核对现有 `raw.ingestion_batch` 或专用的精简凭证能否保住请求身份、来源、捕获时间和校验链。这里先交付选择和依赖对账。

**验收**　每项给出保留正文、只留凭证或待补证的具体结论与依据。实际清理命令与期限另经确认。当前约 59 MiB 的 Raw 和约 22 MiB 的 Ops 从总减容报告中分别计量。

## 回归命令与最终交付

从仓库根目录运行已有隔离测试。每个改动先补能复现该缺口的回归，再在实施后复跑。已有测试覆盖相同场景时沿用原用例。

```powershell
backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py --postgres pytest backend/tests/database_api backend/tests/stock_update backend/tests/marts backend/tests/test_finance_evidence.py backend/tests/test_finance_mcp.py backend/tests/test_portfolio_conditional.py -q --tb=short
backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py --postgres pytest backend/tests/e2e/test_stock_update_postgres.py backend/tests/e2e/test_release_postgres.py backend/tests/e2e/test_database_backup_postgres.py backend/tests/e2e/test_backup_project_guards_postgres.py backend/tests/e2e/test_backup_constraint_names_postgres.py -q --tb=short
backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py --postgres pytest backend/tests packages/news-collector/tests -q --tb=short
```

交付字段与来源对账、两张 JSON 表的逐键迁移、现行公式清单、最新快照接口对照、网站及桌面联测、备份恢复和退役前后的容量及性能测量。基线有差异时注明原因和影响，测试报告记录实际通过、跳过及未覆盖范围。真实供应商 HTTP 和业务库访问违规计数应为零。
