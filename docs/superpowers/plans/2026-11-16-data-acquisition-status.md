# 数据条目获取状态 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为证券×条目/指标、宏观系列及目录其余内容提供可追查的获取状态和三类审计结果，供本机管理台及消费项目读取。

**Architecture:** 在现有持久采集凭证上新增统一状态摘要表，采集入口完成记账时更新；数据可用性和审计分类在只读服务中按指定视图评估。原始作业、游标、原件及发布快照继续保存。宏观保留 `collectionAudit` 1.0 适配，其他消费按需使用新增查询接口。

**Tech Stack:** Python、FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL；管理台沿用原生 JavaScript，pytest 和现有隔离运行器验证。不增加运行依赖或常驻服务。

**Spec:** [已确认设计](E:/code/investmind-data-platform/docs/superpowers/specs/2026-11-16-data-acquisition-status-design.md)

## Global Constraints

- 仓库根目录为 `E:/code/investmind-data-platform`；实施必须同时读取设计和本计划。
- 中文沟通、说明与界面；代码标识、命令和路径使用英文。
- 股票按“证券 × 数据条目或指标”独立维护；宏观按指标独立维护。
- 数据缺失优先；无可信记录、暂停、运行中、不适用单独说明，不计入三类汇总。
- 不根据数据日期判断是否过期，正常不保证官方最新一期；有限数值 `0` 有效。
- 暂停、运行中信息不清除上一次终态和最近成功；任务入口停用不自动等同来源暂停。
- 当前状态、业务数据视图和发布版本分别表达，历史补采不覆盖无关日常状态。
- 旧宏观 `data`、`meta` 和 `collectionAudit` 合同保持兼容；相邻 InvestMind 仓库只读。
- 文档/状态查询不访问供应商，不触发采集、回填、导入、重算或写入修复。
- 不修改个人持仓、交易、策略参数；不更改已有业务记录的结构来重复存状态。
- 当前工作树有上轮目录修订和用户的 `README.md` 修改。执行前记录归属，使用 using-git-worktrees 建立隔离工作区；将本功能依赖的已授权目录修订完整带入，不遗漏新 `scripts/data_catalog_semantics.py`，不提交用户 README 改动。
- 下文 `Files` 中路径均为仓库根目录下的精确路径；新增文件按其职责创建。迁移文件暂定下一版本，实施前以 Alembic 唯一 head 为准调整 `down_revision`，不创建意外分支。
- 下文简写的 `pytest ...` 命令均通过 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest ...` 执行，不能直接加载用户 `.env`。命令中的新增文件由其所属任务创建。

## Review Focus

- 有旧值但本次采集失败，以及无值且采集失败，必须分别为 failed、missing；Task 1 锁定。
- 完整空事件与未核全空事件不可混同；HTTP 200 错误正文、分页或批量缺项不能扩散成功；Task 4/6 锁定。
- 旧尝试晚完成、历史窗口成功和无关证券成功不能覆盖当前失败；Task 2/3 锁定。
- 当前 Core 有值而指定发布版本缺值、相同条目使用不同来源时不能借用证据；Task 7 锁定；值日期旧本身不得触发失败。
- 状态表未覆盖、迁移期旧凭证、无记录以及状态查询故障不能让旧宏观全站变正常或改变业务快照；Task 8/10 锁定。

## 文件职责和交付边界

新增 `backend/app/acquisition/`，仅组织获取证据和审计，不能成为业务计算的新实现。

| 文件 | 职责 |
| --- | --- |
| `contracts.py`、`rules.py` | 明确身份、范围、证据、可用性、分类与合并规则 |
| `catalog.py`、`bindings.json` | 逐目录绑定对象、独立指标、来源、依赖、可用性适配器及采集入口 |
| `repository.py`、`tracking.py` | 单表摘要、尝试身份、条件更新、同事务完成及安全失败记账 |
| `adapters/stocks.py`、`macro.py`、`markets.py`、`content.py`、`imports.py` | 从实际持久记录形成严格证据，不发请求、不另写数据 |
| `availability/stored.py`、`published.py`、`derived.py` | 按请求视图读取实际内容及必要依赖，不以表存在代替有值 |
| `service.py`、`routes.py` | 有界查询、快照内一致读取、只读返回和错误边界 |
| `macro_compat.py`、`initialization.py` | 旧宏观适配及幂等历史初始化，不能互相递归调用 |
| `backend/app/db/models/acquisition.py` | 新增状态表 ORM；现有 schema 和业务表保持原用途 |
| `backend/app/cli/data_status.py` | 初始化报告及显式应用入口 |

任务之间按下述接口协作；跨域模块可拆小文件，但不得改变这些公共函数的参数与语义。

### Task 1：身份、分类器与完整目录绑定

**Files:** Create `backend/app/acquisition/{__init__.py,contracts.py,rules.py,catalog.py,bindings.json}`、`scripts/audit-data-status-bindings.py`、`backend/tests/acquisition/{__init__.py,test_rules.py,test_bindings.py}`。

**Interfaces:**

- `StateKey(entry_id: str, item_id: str, subject_kind: str, subject_key: str, scope_kind: Literal['routine','historical'])`，冻结、可排序；整个条目的 `item_id=''`。
- `Attempt(key: StateKey, attempt_id: str, started_at: datetime, scope: dict, trigger: str)`；scope 仅含已验证的对象、窗口、来源/合同、父计划身份。
- `Evidence(status: str, checked_at: datetime|None, success_at: datetime|None, error_code: str|None, reason: str, scope: dict, references: tuple[dict,...])`；status 限于旧五值。
- `Availability(kind: Literal['available','confirmed_empty','missing','unknown','not_applicable'], period: str|None, reason: str, source: str|None, version: str|None)`；布尔值不作为数值零。
- `Assessment(status: Literal['normal','failed','missing','excluded'], reason: str)`。
- `EvidenceRecord(attempt: Attempt, evidence: Evidence)`；所有适配器及初始化使用此协议，不分别拼装不兼容的成功字典。
- `Binding(entry_id: str, item_id: str, subject_kind: str, source_adapter: str, availability_adapter: str, dependencies: tuple[str,...], writers: tuple[str,...], unavailable_reason: str|None)`；依赖标识为显式 entry/item，不能仅靠英文字段名猜归属。
- `StatusItem(key: StateKey, name: str, context: AuditContext, evidence: Evidence, availability: Availability, assessment: Assessment)`；对外别名为 `collection` 和 `audit`。
- `StatusQuery(entry_ids: tuple[str,...], item_ids: tuple[str,...], subject_kind: str|None, subject_keys: tuple[str,...], scope_kind: str, context: AuditContext, limit: int=25, cursor: str|None=None)`、`StatusPage(schema_version: str, read_at: datetime, items: list[StatusItem], next_cursor: str|None)`。
- `AuditContext` 按 Task 7 的签名在 contracts.py 定义，Task 7 消费它；所有时间须带 UTC 时区。StatusQuery/Page/Item 的 camelCase 序列化别名在本任务固定，外部 key 为 `entryId/itemId/subjectKind/subjectKey/scopeKind`，其余字段对应 Task 9 信封；宏观旧合同继续独立适配。
- `classify(availability: Availability, evidence: Evidence) -> Assessment`；`classify_derived(availability: Availability, dependencies: tuple[Assessment,...]) -> Assessment`；`summarize(items: list[Assessment]) -> dict[str,int]`，含四类数量。
- `load_bindings(root: Path) -> tuple[Binding,...]`、`validate_bindings(root: Path) -> list[dict]`，Binding 声明 entry/item、subject、source adapter、availability adapter、依赖及实际 writer 函数名。所有后续查询和适配只使用登记绑定。

- [ ] 写失败测试，使用下表作为参数化断言；derived 依赖及普通项顺序和网站当前函数一致。

| 可用性 / 执行证据 | 预期 |
| --- | --- |
| available / success | normal |
| available / failed | failed |
| missing / failed | missing，原因包含原错误 |
| confirmed_empty / success | normal |
| available / unrecorded | excluded |
| missing / paused 或 running | excluded |
| not_applicable / success | excluded |
| derived 有值、必要依赖 failed/missing | 依次 failed/missing |
| derived 无值、依赖 failed | missing |

至少提供 `test_missing_has_priority_and_keeps_error`、`test_excluded_not_in_three_categories`、`test_dependency_priority_matches_website`、`test_catalog_binding_covers_every_entry`。前两项断言形状为 `classify(Availability(kind='missing', period=None, reason='无值', source=None, version=None), failed_evidence).status == 'missing'` 且原因保留其错误码，以及 `summarize([normal,failed,missing,excluded]) == {'normal':1,'failed':1,'missing':1,'excluded':1}`。不按日期推断滞后；数值到可用性的有限值检查由 Task 7 锁定。
- [ ] 运行 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest backend/tests/acquisition/test_rules.py backend/tests/acquisition/test_bindings.py -q`，确认缺实现失败。
- [ ] 实现契约和纯分类器；逐项填写当前目录绑定，不创建“所有条目默认成功”的兜底。日行情、分红按条目；三种股息率及现行筛选指标用独立 item；宏观专题子系列不能只绑定整组。历史公式、无供给内容标明原因。
- [ ] 实现离线盘点脚本，输出每项对象、读证据位置、写入口、字段可用性和依赖。校验目录全集有归属、依赖无环、不能用整池 JobRun 给全部证券背书。脚本不访问数据库或网络。
- [ ] 重跑上述测试及 `python -I scripts/audit-data-status-bindings.py`，要求缺失绑定/孤立 item/循环依赖均为 0。提交 `feat: define acquisition status contracts and catalog bindings`。

### Task 2：统一状态表、迁移和条件写入

**Files:** Create `backend/app/db/models/acquisition.py`、`backend/migrations/versions/20261116_0022_data_acquisition_state.py`、`backend/app/acquisition/repository.py`、`backend/tests/acquisition/{conftest.py,test_repository.py,test_migration.py}`；Modify `backend/app/db/models/__init__.py`。

**Interfaces:** `StateRepository(session: Session)`；`begin(attempt: Attempt) -> bool`、`finish(attempt: Attempt, evidence: Evidence) -> bool`、`read(keys: tuple[StateKey,...]) -> dict[StateKey,Evidence]`。方法不自行 commit，事务由调用者管理。conftest 提供 `status_factory`（注册全部 ORM 的内存 SQLite）及冻结 UTC 时钟 `status_now`。

- [ ] 写失败测试：同股不同条目、同条目不同股票、routine/historical 分行；相同尝试幂等；较旧尝试晚完成返回 False；失败后 success_at 保留；事务回滚后新凭据/状态均不提交；未知或无时区时间拒绝。
- [ ] 运行 `pytest backend/tests/acquisition/test_repository.py backend/tests/acquisition/test_migration.py -q`，确认失败。
- [ ] 创建 `ops.data_acquisition_state`，唯一键为 StateKey 五字段；保存最后终态及其 scope、active attempt、开始时间、checked/success 时间及成功范围、计数、错误及引用。暂停是读时配置，不覆盖历史终态。存在必要失败子范围时保留其引用，不能仅用最后一次 HTTP 结果清除整个条目错误。
- [ ] 条件更新使用数据库条件及现有发布/采集锁，不用进程内字典仲裁；旧尝试不得清除新 active attempt。finish 仅接受 success/failed/unrecorded 终态；running 由活跃尝试投影，paused 由来源配置投影。主键排序和查询索引支持条目/对象有界读取。禁止向 scope/reason 存 Token、原文或连接串。
- [ ] 验证 SQLite 行为及隔离 PostgreSQL 的升级、索引、回滚/并发；迁移只新增表和索引，不扫描或改写 Core/Mart。测试通过后提交 `feat: persist acquisition states with attempt fencing`。

### Task 3：公共追踪边界、范围仲裁和恢复

**Files:** Create `backend/app/acquisition/tracking.py`、`backend/tests/acquisition/test_tracking.py`；Modify Task 2 的 repository，必要时复用 `backend/app/jobs/locks.py`。

**Interfaces:** `start_attempt(session: Session, attempt: Attempt) -> bool`；`finish_attempt(session: Session, attempt: Attempt, evidence: Evidence) -> bool`；`record_failed_attempt(factory, attempt: Attempt, *, code: str, reason: str, checked_at: datetime, references: tuple[dict,...]) -> None`；`recover_attempts(factory, *, now: datetime, active_attempt_ids: set[str]) -> dict`。

- [ ] 写失败测试：在源成功凭据提交失败时不得报告 success；独立安全失败事务保留错误；未实际执行的预算跳过/锁忙不会覆盖旧终态；旧历史补采不清除 routine 错误；仅覆盖并验证失败必要范围的重试可解除该错误。
- [ ] 用隔离运行器执行 `pytest backend/tests/acquisition/test_tracking.py -q`，确认失败。
- [ ] 实现公共函数，调用 Task 2 方法；范围来自冻结计划，不用返回行的日期跨度伪造请求范围。恢复只将已确认失联尝试转成 unrecorded 并保留原因，不按业务数据日期推断；不在 GET 中执行恢复。
- [ ] 验证一次 HTTP 多对象、部分子项失败及幂等重入，不能传播到未请求对象。测试通过后提交 `feat: track scoped collection attempts and recovery`。

### Task 4：股票采集、分页和公告/财报凭证接入

**Files:** Create `backend/app/acquisition/adapters/{__init__.py,stocks.py}`、`backend/tests/acquisition/test_stock_tracking.py`；Modify `backend/app/stocks/update/{repository.py,sync.py}`、`backend/app/ingestion/{pipeline.py,pagination.py}`、`backend/app/stocks/financial_supply.py`、`backend/app/content/announcement_history.py`、`backend/app/stocks/classification/sync.py`、`backend/app/stocks/business_supply.py`。只在实际凭证提交边界接入，不改数据获取算法。

**Interfaces:** `read_stock_evidence(session: Session, keys: tuple[StateKey,...], *, now: datetime) -> dict[StateKey,Evidence]`；`bind_stock_attempt(plan, *, identity: str, attempt_id: str, started_at: datetime, scope_kind: str) -> tuple[Attempt,...]`。返回对象必须能从请求/冻结清单证明，batch 全市场模式不能只凭入库存在推断个股查询完成。

- [ ] 写失败测试：600519 日行情成功/分红失败独立，000001 分红不受影响；HTTP 200 错误体、字段/证券错配、后续页失败、原件哈希不符、落库回滚均不成功；分页闭合后确认空分红可用；无覆盖凭证的空分红缺失。
- [ ] 运行 `pytest backend/tests/acquisition/test_stock_tracking.py backend/tests/stock_update/test_repository.py -q`，确认新断言失败而原行为基线可辨认。
- [ ] 在实际网络异常捕获位置登记该单元失败，不能只修改 ingest_unit 而漏掉入库前请求失败。分页在终页证明及全部必要页完成后提交成功；缓存复用标记为本地检查并保留原件时间。
- [ ] 财报复用 `financial-statement-evidence` 的合同、批次、原件及 has_more 核验；公告复用独立证券/日期窗凭证；主营、分类映射按 Task 1 writer 清单接入。历史 backfill 通过通用分页边界标 historical，不改变常规检查。
- [ ] 验证 Task 1 股票域所有活跃来源有写入口或严格既有凭证适配。执行 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest backend/tests/acquisition/test_stock_tracking.py backend/tests/stock_update backend/tests/marts/test_announcement_history.py -q`。通过后提交 `feat: track per-security stock dataset acquisition`。

### Task 5：宏观基础系列与专题执行接入

**Files:** Create `backend/app/acquisition/adapters/macro.py`、`backend/tests/acquisition/test_macro_tracking.py`；Modify `backend/app/macro/jobs/{macro_series_state.py,macro_sync.py}`、`backend/app/macro/extensions/store.py`、`backend/app/macro/{official_macro_store.py,fiscal_store.py,exchange_summary_store.py,official_member_store.py,catalog_member_store.py,rea_input_store.py,receivables_store.py,issuer_recovery_store.py,fund_member_graph_store.py,fund_contract_store.py,issuer_sample_store.py,target_execution_store.py,target_evidence_store.py,sz_market_sample_store.py}`。这些文件按 Task 1 已确认的凭证/终态边界选择接入，调用者共用同一终态写入时只接入一次，不重复嵌套。

**Interfaces:** `read_macro_evidence(session: Session, keys: tuple[StateKey,...], *, now: datetime) -> dict[StateKey,Evidence]`；适配不得调用统一服务或宏观兼容适配器，避免循环。

- [ ] 写失败测试：CPI 成功与 PPI 失败独立；官方多文件计划缺一项为 unrecorded 而非成功；确定错误仍为 failed；恢复只覆盖同一文件槽；目录查核成功且无新候选按既有正式凭据处理；不同交易所和新房/二手房子系列保持独立身份。
- [ ] 运行 `pytest backend/tests/acquisition/test_macro_tracking.py backend/tests/macro/test_collection_audit.py -q` 的隔离入口，确认新断言失败。
- [ ] 在现有 mark_success/真实 mark_stale、reserve/receive/fail/replay 的完成凭据边界使用 Task 3。`MACRO_VALUE_EXPIRED`、`MACRO_HISTORY_GAP` 和未实际执行的预算状态不转换成真实获取失败。
- [ ] 合同允许的备用来源成功可形成条目成功，实际采用来源及失败渠道引用保留；不以不可用主源的一次请求失败覆盖合法 fallback 的成功。宏观周任务总结果不能扩散到所有系列。
- [ ] 测试旧时刻/未来时间、历史补采和正在执行的指定 collectionSeriesIds，只影响相应范围。通过后提交 `feat: track macro series and scoped official collection`。

### Task 6：市场品种、新闻及外部文件接入

**Files:** Create `backend/app/acquisition/adapters/{markets.py,content.py,imports.py}`、`backend/tests/acquisition/{test_market_tracking.py,test_content_tracking.py,test_import_tracking.py}`；Modify `backend/app/market/{data,pulse,region,etf,commodities}/{repository.py,sync.py}`、`backend/app/content/{news_runtime.py,news_ingestion.py}`、`backend/app/ingestion/external_import.py`。

**Interfaces:** `read_market_evidence(...)`、`read_content_evidence(...)`、`read_import_evidence(...)` 参数及返回类型与 Task 5 read_macro_evidence 一致。

- [ ] 写失败测试：指数/ETF/期货按代码独立；ETF 价格成功而份额失败独立；地域结果无效不能成功；新闻真实请求错误和入库交付错误均可追查，而仅 SOURCE_STALE 质量告警不冒充新请求失败；合法零新增新闻检查成功；导入 dry-run 不写获取状态，坏文件不产生成功凭据。
- [ ] 执行 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest backend/tests/acquisition/test_market_tracking.py backend/tests/acquisition/test_content_tracking.py backend/tests/acquisition/test_import_tracking.py backend/tests/market_data backend/tests/market_pulse backend/tests/market_region backend/tests/market_etf backend/tests/market_commodities backend/tests/marts/test_news_collector.py backend/tests/marts/test_news_unified.py backend/tests/external_files -q`，确认新增断言失败。
- [ ] 复用现有原件身份、请求参数、内容哈希及 Core 绑定。HTTP 异常在 sync 层和解析拒收在 repository 层均通过同一追踪服务归入对应尝试，不重复计数。新闻分别保留来源检查与内容交付/发布范围，未成功入库不能报该存储条目正常。
- [ ] 导入以来源/内容类型及文件哈希建立范围；只从被接受记录映射对象，无法识别对象的坏文件只记导入范围错误，不扩散到所有系列。提交前覆盖 Task 1 剩余活跃 writer 清单，既有历史兼容内容明确说明。
- [ ] 验证相应域测试通过，提交 `feat: track market content and imported data acquisition`。

### Task 7：入库/发布可用性、独立指标与依赖评估

**Files:** Create `backend/app/acquisition/availability/{__init__.py,stored.py,published.py,derived.py}`、`backend/app/acquisition/service.py`、`backend/tests/acquisition/{test_availability.py,test_dependencies.py}`。

**Interfaces:**

- `AuditContext(view: Literal['stored','published'], product: str|None=None, version: str|None=None, period: str|None=None)`。
- `read_stored(session: Session, keys: tuple[StateKey,...], *, now: datetime) -> dict[StateKey,Availability]`。
- `read_published(session: Session, keys: tuple[StateKey,...], context: AuditContext) -> dict[StateKey,Availability]`。
- `assess_keys(session: Session, keys: tuple[StateKey,...], context: AuditContext, *, now: datetime) -> list[StatusItem]`，StatusItem 包含 key、name、context、evidence、availability、assessment。
- `StatusService(factory, *, root: Path, clock: Callable[[],datetime])`；`query(request: StatusQuery) -> StatusPage`。一个查询在只读一致性事务内读取当前证据和选定视图，批量查询不得逐项重复加载所有原件。

- [ ] 写失败测试：stored 有价格而 published 指定版本缺值；三种股息率仅一项有效；历史有效值与当前成功检查可正常；实际来源/对象/字段期不匹配为 excluded；零值有效但 bool、NaN、无穷无效；单项无可用映射不能降级借 Core 值。
- [ ] 运行 `pytest backend/tests/acquisition/test_availability.py backend/tests/acquisition/test_dependencies.py -q`，确认失败。
- [ ] 按 Task 1 bindings 使用现有 DTO/构建器的已定义字段，集中完成各存储域的有界查值；不重新实现股息率或企业公式。事件空值使用已证明的查询覆盖；仅 table/count>0 不足以证明被问字段可用。
- [ ] 递归依赖使用 Task 1 分类器并缓存同次查询结果；复用实际成员/证券、报告期和适用性规则。风险、宏观差值、市场聚合及 portfolio 公共输入按自身结果和必要依赖评估；未知依赖保留 excluded，不冒充供应商成功。
- [ ] 验证测试通过及查询无写入/HTTP，提交 `feat: assess stored and published data acquisition evidence`。

### Task 8：旧宏观合同兼容适配

**Files:** Create `backend/app/acquisition/macro_compat.py`、`backend/tests/acquisition/test_macro_compat.py`；Modify `backend/app/macro/audit.py`、按需 `backend/app/main.py`；既有 `backend/app/macro/routes.py` 的返回语义不改。

**Interfaces:** `read_compatible_macro_audit(factory, legacy_reader: Callable[[],dict], *, now: datetime) -> dict`。legacy_reader 必须是原始凭证读取层，不能再次调用 CollectionAuditReader 包装层；新表仅替代已接入、可证明完整的对应项。

- [ ] 写失败测试，固定完整旧数据 fixture，逐字段比较 id、enabled、五状态及时间；同一旧快照两次读取分别附当前成功/失败而 data/meta 相同；新表空/缺表仍从可信旧凭证读取；无可信新旧凭证保持 unrecorded；读取整体失败保持 collectionAudit:null、不泄漏异常正文。
- [ ] 隔离执行 `pytest backend/tests/acquisition/test_macro_compat.py backend/tests/macro/test_collection_audit.py -q`，确认缺兼容实现失败。
- [ ] 接入新表前检查当前范围权威性与尝试时序，不能仅凭“有行”优先于更晚的旧凭证。旧质量/预算标记继续原转换；新任务开关不改变旧 enabled 来源语义。
- [ ] 用相邻网站 `src/modules/macro/audit/collection.ts` 的现有 readCollectionAudit 校验最终样例；加载其纯 TypeScript 解析器做契约验证，不修改网站文件或安装依赖。旧返回 schemaVersion='1.0'、basis='current-collection-records'，不向 status 塞入 normal/missing。
- [ ] 两边既有宏观审计样例与本仓回归通过后提交 `feat: preserve macro collectionAudit compatibility`。

### Task 9：统一只读查询接口

**Files:** Create `backend/app/acquisition/routes.py`、`backend/tests/acquisition/test_status_routes.py`；Modify `backend/app/main.py`。

**Interfaces:** `create_router(service: StatusService) -> APIRouter`，注册 `GET /api/v1/data-status`。StatusQuery 支持重复 `entryId`、重复 `subjectKey`（分别最多 20/100、展开组合最多 100）、`subjectKind`、`scopeKind='routine'`、`view='stored'`、`product`、`version`、`period`、`limit=25`（1..100）、`cursor`。item 过滤名 `itemId` 可重复且纳入展开上限。

返回 `{schemaVersion:'1.0',readAt,items,nextCursor}`。item 采用 `{key,name,context,collection,availability,audit}`；key 五字段、collection 含五状态/时间/原因/范围/受限引用、audit 为 status/reason。不得混用 DataMeta 发布 version 表示当前状态版本。

- [ ] 写失败测试：单证券多个条目、单条目多证券、宏观；重复参数去重；超限/非法代码/坏游标 422；未知目录 404；已知但无记录仍返回明确条目及 excluded/missing；stored 与 published 区分，stored 携带 version/product 报 422；数据库断连 503；不发 HTTP、不写数据库。
- [ ] 隔离执行 `pytest backend/tests/acquisition/test_status_routes.py -q`，确认失败。
- [ ] 稳定排序采用 StateKey 五字段；游标绑定过滤参数指纹且校验长度/类型，筛选改变后旧游标报 422。published 需要 product，version 缺省时取当前指针并返回实际版本；映射不支持则单项 excluded，产品/指定版本不存在为 404。
- [ ] 基于 bindings 和已配置对象集合生成有界候选，因此没有状态行也可被查到；禁止查询无界全市场。响应 no-store，错误码和摘要脱敏。
- [ ] 验证路由与现有 v1 API 回归后提交 `feat: expose bounded data acquisition status queries`。

### Task 10：历史凭证初始化、恢复命令和迁移验证

**Files:** Create `backend/app/acquisition/initialization.py`、`backend/app/cli/data_status.py`、`backend/tests/acquisition/test_initialization.py`；Modify `backend/app/cli/main.py`、现有调度启动恢复入口 `backend/app/jobs/scheduler.py`。

**Interfaces:** `plan_initialization(factory, *, root: Path, now: datetime, limit: int, cursor: str|None) -> InitializationPage`；`apply_initialization(session: Session, records: tuple[EvidenceRecord,...]) -> dict`。CLI `reconcile-data-status` 默认只输出报告；`--apply` 显式写已验证摘要，`--limit` 默认 100/最大 1000，分页可续跑。

- [ ] 写失败测试：只有业务值没有凭证；仅整池成功；可验证旧分页终态；坏哈希/未来时间/时间倒序；已暂停记录保留原事实；中断结果待确认；重复运行不改原 checked/success 时间；较旧初始化不能盖过实时新失败。dry-run 及 HTTP GET 都无写入。
- [ ] 隔离执行 `pytest backend/tests/acquisition/test_initialization.py -q`，确认失败。
- [ ] 使用 Task 4..6 适配器输出同一 EvidenceRecord 协议（attempt + evidence），仅 apply 使用 Task 2 同事务写入。记录原事件时间，CLI 执行时间只作报告 readAt；无须供应商和业务重建。初始化分页每次限额，不能读入所有 source_payload 正文再筛选。
- [ ] 调度现有恢复位置调用 Task 3 的 recover_attempts，只在恢复器有证据确认失联时降为 unrecorded。CLI/manual 无法确认的运行状态单独说明；不因读取时长或业务日期而伪造失败。
- [ ] 隔离 PostgreSQL 验证 Alembic upgrade 与初始化，比较 raw/core/derived/mart 行内容与指针未变。新表不存在时初始化明确要求迁移，不自动建表或忽略错误。通过后提交 `feat: reconcile acquisition states from existing evidence`。

### Task 11：管理台显示、目录能力和操作文档

**Files:** Modify `backend/app/local_admin/{routes.py,static/catalog.js,static/admin.css}`、`scripts/export-data-catalog.py`、`scripts/data_catalog_semantics.py`、`docs/local-operations.md`、`docs/platform-api.md`；Create `backend/tests/acquisition/test_admin_status.py`；同步生成的 `docs/data-catalog*.json`。

**Interfaces:** 管理子应用新增 `GET /admin/api/data-status`，复用 Task 9 参数与同一个 StatusService，不转发 HTTP 回自身。静态 catalog 只登记状态绑定/查询能力，不存运行结果。

- [ ] 写失败测试：管理接口限定本机，默认 stored；状态库不可用不阻塞目录正文；股票过滤状态独立；表/运行链接使用当前 origin；单独说明有独立数量，总数=normal+failed+missing+excluded，注明当前筛选/页内统计。
- [ ] 隔离执行 `pytest backend/tests/acquisition/test_admin_status.py backend/tests/test_admin_catalog.py backend/tests/test_admin_catalog_routes.py backend/tests/test_local_admin_routes.py -q`，确认新功能断言失败。
- [ ] 在现有条目详情增加独立异步状态区，支持证券/品种筛选、独立 item、分页；显示中文分类、读时刻、检查/成功时间、实际范围、已有数据/确认空、原因及受限运行链接。切换条目丢弃旧异步响应，不新建原型站点。
- [ ] 记录新增查询合同与 CLI 命令，明确普通接口兼容、正常不等于最新、历史状态的证据边界与安全部署顺序。更新离线导出和覆盖校验，已有 catalog validator 不再假定 v1 只有十个接口。
- [ ] 浏览器验证 11383 及测试端口、窄视口、数据库异常和跨页响应，JS 语法及相关回归通过后提交 `feat: show per-object acquisition status in local catalog`。

### Task 12：全链路验收、独立复核和上线准备

**Files:** Create `backend/tests/acquisition/test_status_integration.py`、`backend/tests/e2e/test_acquisition_status_postgres.py`；更新本计划完成记录与实际覆盖报告。只有新问题才修改前述模块。

**Interfaces:** 复用各 Task 已定义接口；交付真实数据仍由已有采集任务提供，新测试使用可控来源和隔离库。

- [ ] 先写集成断言并确认未覆盖路径会失败：源成功→提交→统一状态→查询→管理显示；保留旧值后源失败→failed；无值源失败→missing；修复同范围→normal；暂停/运行/不适用→excluded 且不进入三类；强制 commit 失败不产生假成功。
- [ ] 运行 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest backend/tests/acquisition backend/tests/macro/test_collection_audit.py backend/tests/test_admin_catalog.py backend/tests/test_admin_catalog_routes.py backend/tests/test_local_admin_routes.py backend/tests/test_domain_boundaries.py -q`。再按实际改动域运行原有 stocks/market/news/import 定向测试；通过后不无理由重复全库。
- [ ] 用运行器登记的一次性 PostgreSQL 跑迁移、并发、初始化及指针不变测试，命令为 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py --postgres pytest backend/tests/acquisition/test_migration.py backend/tests/e2e/test_acquisition_status_postgres.py -q`。只能由运行器登记 `postgres:17-alpine` 容器，不连接 `.env` 业务库冒充隔离测试。
- [ ] 执行绑定全集校验、导出可重复性、`node --check backend/app/local_admin/static/catalog.js`、`git diff --check`；确认没有网站仓修改、用户 README 改动或临时原型进入提交。
- [ ] 按所选执行技能完成最终独立复核；修正事实/实现问题后只重跑受影响验证。交付覆盖清单逐条对应适配/依赖/未供给原因，不将空表或未接入域当作完成。
- [ ] 准备本机部署的具体迁移及回执初始化报告，核对任务已停用或在途已结束、目标数据库与备份；在已授权范围内完成迁移、初始化和进程重启。状态上线不能触发真实供应商采集。复查旧宏观接口 data/meta、当前指针、各任务开关及新状态页面，记录实际结果；运行条件不满足时保留待部署项，不宣称上线。
- [ ] 提交 `test: verify acquisition status integration and compatibility`，报告完成项、验证结果及未确认凭证范围，不承诺官方最新数据。

## 计划自检与当前交接

分类/身份/全集绑定由 Task 1 覆盖；单表迁移/提交/并发由 Task 2/3 覆盖；所有来源域由 Task 4..6 覆盖；可用性/派生/范围由 Task 7 覆盖；旧站兼容由 Task 8、新查询由 Task 9、历史恢复由 Task 10、管理台由 Task 11、集成及部署由 Task 12 覆盖。

执行前须将 Task 1 生成的逐项清单并入本计划附件，实施中按真实文件名细化域内接入，不能静默缩减范围。所有复用函数以 Interfaces 块为准；后续改签名必须同步调用任务及测试。

用户已选择同一实施者执行、独立复核。实际覆盖清单在 `backend/app/acquisition/bindings.json`，可运行 `scripts/audit-data-status-bindings.py` 按条目和指标核对。共登记 141 个目录条目、228 个绑定。历史初始化按已有证据覆盖，并非每个登记项都能恢复一次历史执行。

| 范围 | 实际依据 |
| --- | --- |
| 证券和交易所 | 证券代码、接口、冻结请求/窗口、批次或分页，财报另核对各证券的报表计划。北交所映射只覆盖返回代码对。 |
| 宏观与市场 | 基础序列状态、来源及目录版本，扩展或交易所记录按系列、文件或品种归属。深圳市场单次采样保留原始样本用途。 |
| 公告、新闻和外部文件 | 公告完结窗口、新闻来源采集与交付状态、已接受文件的类型和来源。导入文件校验失败仍可在 `raw.external_file` 查到；文件身份不足时无法归因给某个指标。 |
| 计算与发布 | 读取实际结果、可验证输入和所选产品版本。企业范围核对目标、样本和财报凭证，无法从快照还原的成员范围列入单独说明。 |
| 历史初始化 | 读取宏观、冻结股票请求、市场和导入文件凭证。仅有业务值或汇总作业成功时，保留缺少可信执行记录的说明。 |

复核已发现并修正跨来源和跨窗口误判、显式分页未结束、同轮新闻发布失败、公告空窗口、企业成员与实际结果、单证券分页及外部来源兼容问题。隔离 PostgreSQL 验证了迁移、并发及初始化的时序保护。

## 本机部署验收记录

2026-11-16，本机数据库 `danginvest` 从 `20261116_0021` 升级至 `20261116_0022`。迁移前的备份编号为 `20261116T144605Z-2e31eccbbc384a1894cbe803c8452aea`，归档校验通过。历史初始化扫描 1,267 条输入，处理 1,448 条可归属回执，形成 487 行状态。重复初始化写入 0 行。

管理台与消费接口均已在本机 11383 端口验证。部署前后 65 张原有表的写入计数、56 个发布指针、宏观接口的 `data` 和 `meta` 均一致。InvestMind 的原解析器读出了相同的 183 个审计项。9 个定时任务保持关闭，部署和验收均未发起供应商采集。

逐项检查还发现指数基础资料、指数成分权重与 ETF 基础资料使用独立的对象登记和原始凭证。状态查询已接入这些依据。已登记对象可以显示缺失。只取得部分权重候选时，页面保留未证明完整成分的说明。
