# 统一使用现有 PostgreSQL 的实施计划

> 执行者按下列复选框逐项验收。分任务实施使用 `superpowers:subagent-driven-development`，同一执行者连续实施使用 `superpowers:executing-plans`。

**目标**　将本机定时任务状态和执行历史迁入当前 PostgreSQL，完成新闻入口、运行代码、测试与项目文件的旧存储清理。

**架构**　由 Alembic 在现有主库新增两张管理表。本机父子进程通过数据库事务协调任务，新闻使用项目已有队列、游标与凭证。数据库测试只连接本次测试创建的 PostgreSQL。

**技术栈**　PostgreSQL 17 测试容器、SQLAlchemy、psycopg、Alembic、pytest。实施基线为 `379f284`。[已确认的设计](../specs/2026-11-16-postgresql-only-design.md)

## 全局约束

- 真实迁入目标取自 `DANG_DATABASE_URL`。保留原任务配置、历史 Alembic 修订号、业务作业记录和新闻投递语义。
- 在隔离库验证竞态、失败和重试。在实际库停写期间单独记录迁入前后结果。记录按键比较，可公开的回执省略连接凭据和原始正文。
- 以下测试命令从仓库根目录执行，先在 PowerShell 设置 `$py = (Resolve-Path backend/.venv/Scripts/python.exe).Path`。每组测试先复现行为差异，实施后重跑该组，按任务单独提交已通过的修改。

## 审查重点

| 场景 | 预期 | 对应任务 |
|---|---|---|
| 两个测试或子进程同时写库 | 同一用例可共享数据，用例间相互隔离，业务连接被拒绝 | 1 |
| 两个工作进程同时领取，或旧父进程丢失锁 | 同一轮最多领取一次，过期工作进程无法结算新轮次 | 3 |
| 采集成功后发布失败 | 新闻仍待交付，同轮的交付失败可在状态查询中看到 | 4 |
| 已有凭证发生竞争写入，或公告恰在查询时间边界公开 | 较旧尝试无法覆盖新凭证；同一读取时刻的查询结论一致 | 2、5、7 |
| 导入中断、重复导入或碰到同键异值 | 可以重试，相同记录只导入一次，冲突回滚，原件保留到核验通过 | 6、7 |

## 1．建立 PostgreSQL 测试隔离

- [x] 修改 `backend/tests/run_isolated.py`、`backend/tests/isolation_probe.py`，新增 `backend/tests/support/postgres.py` 和共享 pytest fixture。测试会话复用一次性 PostgreSQL 17 容器，每个用例按需分配独立数据库。提供 `pg_url: str` 给需要多连接及子进程的用例；需要多个目标的用例可再分配库。测试空库、部分表或历史版本时由用例自行初始化。
- [x] 登记临时实例和用例数据库的精确连接身份。保留真实 HTTP、数据库工具和未登记连接的拦截。独立子进程仅获得本用例的测试连接，测试结束释放连接与临时资源。验证 `select 1` 成功、两个库互不可见、业务连接探针在发起网络请求前被拒绝。
- [x] 先迁移 `backend/tests/db/test_session.py` 与 `backend/tests/acquisition/conftest.py`。运行 `& $py -I backend/tests/run_isolated.py --postgres pytest backend/tests/db/test_session.py backend/tests/acquisition/test_repository.py -q`，检查锁、时间和五列联合键的真实行为。

## 2．添加两张管理表并验证升级

- [x] 在 `backend/app/db/models/` 增加本机任务和执行记录模型，在 `__init__.py` 登记。对照 `backend/app/local_admin/control.py` 的全部字段定义布尔值、可空字段、带时区时间及原始执行 ID。为任务名设主键、执行 ID 设可显式导入的自增主键，为按任务倒序分页建索引。
- [x] 新建 `backend/migrations/versions/20261117_0023_local_admin_control.py`，父修订为 `20261116_0022`（实施前核对唯一 head）。更新 `20260817_0001_data_foundation.py` 的 `OWNED_LATER`、`backend/app/ops/backup/project_guards.py` 和断言最新 head 的测试。历史特定版本测试仍指向原修订。
- [x] 在 `backend/tests/e2e/test_acquisition_status_postgres.py` 或同目录专项用例分别演练空库升到 head、已有凭证和快照的 0022 升到 head。断言管理表在 0022 尚不存在，升级后同时存在，原凭证各列和发布指针相等，备份恢复工具认可新版本。运行该组、`backend/tests/acquisition/test_migration.py` 和 `backend/tests/ops/test_release.py`。

## 3．切换本机管理父子进程

- [x] 修改 `backend/app/local_admin/control.py`、`workers.py`、`backend/app/jobs/scheduler_control.py`、`scripts/start-project.py`。将 `ControlStore(path)` 改为 `ControlStore(session_factory)`；子进程通过管理模式标志识别父进程，从项目配置自行建立写连接。向管理 API 返回与现有页面兼容的时间、结果和分页字段。
- [x] 条件更新领取任务并在同一事务创建执行记录。`admit(...) -> int | None` 返回本轮 ID，`finish(name, execution_id, result)` 精确结束该轮。管理父进程占用专用连接上的 advisory lock 后做冷启动恢复；连接丢失使旧父进程退出管理，旧子进程停止领取。实现与锁所有者关联的有效性检查，覆盖新旧父子进程交错的情况。
- [x] 扩展 `backend/tests/test_admin_workers.py` 及调度控制用例。断言双领取只有一个执行 ID、重复补跑不会再次发请求、禁用允许本轮结束、旧轮次完成不会清除新轮次的 active 标志，丢锁与重启可恢复。检查 `/admin/api/tasks` 和 `/admin/api/tasks/{name}/runs` 的 `localRuns`、`runs` 仍分别可读。

## 4．统一新闻采集入口

- [x] 修改 `packages/news-collector/src/smartparse_news/collector.py` 和 `storage.py`，使 `Collector(config, *, store, ...)` 使用注入的 Store。移除本地文件实现、包的 `__main__.py` 和 `packages/news-collector/pyproject.toml` 中的命令注册。更新宿主调用、包测试和包说明。
- [x] 修改 `backend/app/content/news_storage.py`、`news_runtime.py` 和配置。把实际切换所需的旧新闻迁入能力留在任务 6 的一次性工具中。保留 `watch-news`、`sync-news`、`news-status`、队列确认和健康游标更新事务、`save_health` 调用 `record_news` 的先后关系。检查旧 migration 返回字段的使用方，更新相关合同。
- [x] 运行 `backend/tests/marts/test_news_unified.py`、`backend/tests/acquisition/test_review_regressions.py`、采集包测试。用隔离数据库验证发布失败保留 pending、重试成功后确认、分页中途恢复。在重新安装后的环境检查发行包入口元数据，确认已移除额外命令，宿主三个命令仍可发现。

## 5．收敛数据库访问并迁移其余用例

- [x] 统一 `backend/app/db/session.py`、`main.py`、`shared/readonly_database.py` 与 CLI 的连接处理。由 `backend/app/ingestion/external_import.py` 提供纯文件预检并供 `validate-import` 和原 `ExternalImporter.validate` 复用；在数据库不可用时校验合法和不合法文件，核对原输出字段、错误和退出码。
- [x] 逐项核对设计列出的五个 acquisition 文件。保留 PostgreSQL 冲突插入与行锁、JSONB 查询、时区、只读事务、实际 schema 检查。对 `backend/app`、历史迁移、工具代码搜索方言判断、schema 映射、本地锁回退、日期解码和内存引擎构造，保留各自 PostgreSQL 语义。运行 acquisition 全组、宏观审计、状态路由、旧数据及 PostgreSQL 迁移回归。
- [x] 对 `backend/tests`、`packages/news-collector/tests` 中需要数据库的用例，按业务模块把文件路径、内存引擎、底层 SQL 和测试清理改用任务 1 的隔离库。保留请求次数、异常处理、重启与多连接的断言。仅测算法的用例可用已有协议替身。检查发布、备份、宏观、股票、市场、导入及本机启动器的测试命令和文档。
- [x] 从仓库根运行 `& $py -I backend/tests/run_isolated.py --postgres pytest backend/tests packages/news-collector/tests -q`。记录通过、失败和跳过原因；定位因连接配置、测试隔离或读取时刻造成的差异，修整后重跑受影响组。核对受控 HTTP 与数据库隔离探针仍能捕获违规访问。

## 6．演练一次性迁入

- [x] 在临时工作目录准备只用于本次切换的工具。对本机源使用只读一致性快照，按任务名、执行 ID 导入 PostgreSQL；源初始化时的空值、运行中的轮次、时间和原 ID 可追溯。校准自增序列，覆盖空源及历史 ID 有空洞的情况。
- [x] 先核对配置指向的旧新闻文件与主库迁移标记。文件存在时沿用已有 `migrate_legacy` 的来源验证、冲突及回执语义。已迁入且目标已推进时校验原件和既有回执；无旧文件则以主库现状为准。迁入不调用采集或发布逻辑。
- [x] 在一次性 PostgreSQL 上演练含执行历史、未投递新闻、完全相同的重复导入、同键异值与中途失败。每次通过新连接读回逐字段比较，失败后原目标记录完整。确认再次执行不会重置序列或运行状态。实际切换结束后删除一次性工具及相关源格式测试材料。

## 7．迁入当前项目库、恢复服务并清理

- [x] 以实际连接核对目标库身份、迁移版本和源文件位置。记录原进程与开关并停写、等待在途任务结束。按项目手册建立并验证切换前备份，保留旧控制库的一致性恢复副本。按获取状态表五列主键排序记录内容摘要、行数及当前发布版本，固定 UTC 时刻读取代表性对象、数据期与发布版本。
- [x] 在同一目标库应用新迁移，运行任务 6 已演练的导入。恢复服务前逐条核对，复查摘要、发布版本和同一时刻的查询结果。将实测数记入交付回执；9 条任务、0 条历史是前次盘点值。核对失败时保留源和切换前备份，按已提交阶段重试。
- [x] 待核对通过，恢复先前启用的进程。检查管理页、任务历史、新闻队列、公共 `/api/v1/data-status` 和本机 `/admin/api/data-status` 的读取与分页。实时采集引起的状态变化另行记录。
- [x] 清理 `.env.example`、实际本机废弃配置键、Compose 新闻挂载、手册和项目文档中的旧存储说明。逐一识别实际源文件、测试临时库和相应附属文件，核验绝对路径及文件归属再删除。对工作树全文、文件名、配置键和未跟踪数据库产物分别复查，包括脚本、过往计划和测试；排除 Git 对象与第三方安装环境。报告实际迁入数、版本、核对结果、测试结果与剩余项。

## 实际切换回执

2026-11-17 在当前写连接上将数据库从 `20261116_0022` 升级至 `20261117_0023`，两张新增管理表位于 `ops`。

- 停写时有 9 条本机任务状态和 0 条执行历史。迁入后以新连接逐字段核对。旧新闻文件不存在，现有新闻队列按原发布语义保留。
- 按获取状态的五列主键核对 487 条凭证，另核对 56 个当前发布版本。在同一读取时刻比较 7 项证券和宏观查询，覆盖库存、数据期、指定发布版本及分页，前后结果一致。
- 备份 `20261117T021508Z-c500484c747b4676956dc3703ae5b652` 经独立 PostgreSQL 恢复，包含 67 张切换前数据表，结果为 `restore_verified`。本机控制源另有经过逐行比对的用户本地恢复副本。
- 在 `127.0.0.1:11383` 恢复原管理台。核对 9 个任务的开关和历史列表，以及公开和管理 API 各两页。新闻接口在切换前后均返回 `SOURCE_STALE`，采集开关保持原设置。
- 清除项目内 1 份旧控制源及 949 份已确认归属的临时测试文件。新闻库继续通过项目的 `watch-news`、`sync-news`、`news-status` 使用。

## 切换后的表清单

按 PostgreSQL schema 分类，共 69 张表。表名与所在 schema 组合即为完整名称。

管理台从 `ops.local_admin_task` 读取本机定时任务的开关和调度状态，从 `ops.local_admin_execution` 读取每次启动、补跑及结束的历史。`ops.job_run` 记录业务作业的运行结果，`ops.data_acquisition_state` 记录具体数据对象的采集和可用状态。

| schema | 用途 | 表数 | 表名 |
|---|---|---:|---|
| `raw` | 来源原件、导入批次和拒收记录 | 9 | `enterprise_order_response`、`enterprise_rea_response`、`enterprise_receivable_response`、`enterprise_target_observation`、`external_file`、`external_record`、`ingestion_batch`、`rejected_record`、`source_payload` |
| `core` | 标准化的业务事实和主数据 | 32 | `adjustment_factor`、`balance_sheet`、`business_segment`、`cashflow_statement`、`classification`、`classification_member`、`content_document`、`daily_basic`、`daily_price`、`disclosure_schedule`、`dividend`、`enterprise_order_supplement`、`enterprise_rea_supplement`、`enterprise_receivable_supplement`、`enterprise_target_candidate`、`financial_express`、`financial_forecast`、`financial_indicator`、`income_statement`、`index_constituent`、`listing_status_history`、`macro_observation`、`market_daily_quote`、`price_limit`、`repurchase`、`security`、`security_name_history`、`share_unlock`、`shareholder_count`、`suspension`、`top_holder`、`trade_calendar` |
| `derived` | 派生指标、风险和计算输入 | 4 | `macro_xray_result`、`metric_input`、`metric_value`、`risk_result` |
| `mart` | 供查询及发布的产品快照 | 5 | `page_snapshot`、`published_snapshot`、`stock_daily_chart`、`stock_screener_daily`、`stock_workbench_snapshot` |
| `ops` | 作业、调度、采集凭证、质量与本机管理 | 18 | `data_acquisition_state`、`enterprise_order_attempt`、`enterprise_rea_attempt`、`enterprise_rea_plan`、`enterprise_receivable_attempt`、`enterprise_target_execution_attempt`、`enterprise_target_execution_plan`、`job_cursor`、`job_failure`、`job_run`、`local_admin_execution`、`local_admin_task`、`macro_series_state`、`news_delivery`、`operational_alert`、`quality_check_result`、`scheduler_checkpoint`、`scheduler_heartbeat` |
| `public` | Alembic 迁移版本 | 1 | `alembic_version` |

整仓 PostgreSQL 回归执行 7718 项，均通过。发布验收补充了撤销历史指针的反例，两份完整发布测试文件共 30 项通过。最终合入主工作区后，按四个互斥分组再执行当时收集的全部 7720 项，全部通过，跳过和隔离违规均为 0。仅有 Starlette 测试客户端使用 AnyIO 旧别名的弃用提示。
