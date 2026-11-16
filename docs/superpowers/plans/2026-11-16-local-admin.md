# 数据底座本机管理台实施计划

> 实施者按勾选项跟踪进度。逐项实施和独立审阅使用 `superpowers:subagent-driven-development`，在当前会话连续实施使用 `superpowers:executing-plans`。

**目标**　在同一个本机页面查看接口、应用表与采集状态，修改已确认的配置并安全地启停九项业务任务。

**架构**　本机启动器向 FastAPI 单独挂载管理子应用，静态页面与管理接口同源。启动器持有子进程，调度器或新闻进程返回实际应用结果；PostgreSQL 提供只读的业务数据与执行证据。

**技术栈**　沿用 Python、FastAPI、SQLAlchemy、PostgreSQL、APScheduler 和现有隔离测试入口。页面使用 HTML、CSS 与原生 JavaScript。

**设计依据**　[已确认的本机管理台设计](../specs/2026-11-16-local-admin-design.md)

## 全局约束

- 本机入口为 `python scripts/start-project.py`、`127.0.0.1` 及现有端口的 `/admin`。服务器 Compose、公共 OpenAPI 和手动 CLI 入口保持原有合同。
- 接口说明、表预览与状态查询均为只读。配置按文件应用；只有运行方确认后才能显示已生效。全程无需自动重启项目。
- 日程展示频率、触发时刻和时区，轮询单列间隔，运行记录单列实际开始与耗时。按证据呈现进度和数据归属。
- 下列命令从仓库根目录运行。PowerShell 中先定义 `function check-admin { & backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest @args }`。标为 PostgreSQL 的用例使用同一入口的 `--postgres pytest` 模式和测试容器。

## 重点验证

| 条件 | 期望行为 | 负责项 |
| --- | --- | --- |
| 本地浏览器访问其他网站，或管理 URL 收到伪造 Host | 管理请求被拒绝，配置与进程不动 | 1 |
| 空表、重名表、复合键、超大单元格 | 限定 schema 与响应大小，可以翻页并识别无稳定顺序的表 | 2 |
| 两个标签页编辑或外部文件已变更 | 陈旧页面收到冲突提示，保留磁盘较新的字段 | 3 |
| 上一轮正在收尾，同时关闭、重开或重复应用 | 活跃实例唯一，已取消的排队轮次不能开始 | 4、5 |
| 子进程已处理但回执丢失，或历史只有过期的 running | 不重复补采，也不把历史行报成实时运行 | 4、6 |

## 1. 本机入口及接口文档

**文件与接口**　在 `backend/app/local_admin/routes.py` 新建 `mount_admin(app: FastAPI, *, project_root: Path) -> None`，从 `scripts/start-project.py` 的 `serve_local` 显式调用。在 `backend/app/local_admin/static/index.html` 加入导航和文档目录，在 `backend/tests/test_local_admin_routes.py` 覆盖挂载边界。

- [ ] 写 `test_admin_only_on_local_launcher`，断言本机应用可打开页面、Swagger 和项目内接口说明，默认应用的管理地址返回 404，两者公共 OpenAPI 路径相同。增加恶意 Host、跨源 POST、缺少写入凭据及文档目录穿越用例。
- [ ] 执行 `check-admin backend/tests/test_local_admin_routes.py -q`，确认新用例因缺少管理入口失败。
- [ ] 挂载子应用，复用现有公共文档及导出的请求合同。以受限文档映射提供仓库说明，处理其中站内链接。为管理入口校验回环 Host，为写请求校验 Origin 与启动时生成的防跨站令牌；禁用管理响应的跨源读取。
- [ ] 重跑本项测试和 `backend/tests/test_configured_startup.py`，全部通过后单独提交。

## 2. 应用表目录和只读记录

**文件与接口**　新增 `backend/app/local_admin/tables.py`、`backend/tests/db/test_admin_tables_postgres.py`，扩展管理路由。提供 `GET /admin/api/tables` 和 `GET /admin/api/tables/{schema}/{table}?cursor=…`，后者返回 `columns, rows, nextCursor, previousCursor, orderHint`。

- [ ] 在测试库创建空表、两张同名跨 schema 表、含重复日期及复合主键的表、无唯一键表和含大型 JSON 的表。断言默认 25 行、前后翻页、表列说明、无稳定键提示、未知表拒绝访问；只读事务中读取受限长值，展开后能查看后续内容。
- [ ] 用 `backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py --postgres pytest backend/tests/db/test_admin_tables_postgres.py -q` 验证初始失败。
- [ ] 通过数据库目录识别项目应用 schema 的表、列、注释、索引及唯一键。标识符使用反射所得的 SQLAlchemy 对象。按设计选择稳定排序，无稳定键回退有界偏移分页。列表查询限制列值与整页字节数，使用分页标识获取更多长值；无稳定键时将展开标识绑定到该次读取，防止展开成另一行。每次仅查询目标页，多取一行判断是否可继续。
- [ ] 重跑 PostgreSQL 测试，验证数据没有写入、列表无全表计数和大字段整列传输，然后提交。

## 3. 配置读取、校验与冲突处理

**文件与接口**　新增 `backend/app/local_admin/configuration.py` 和 `backend/tests/test_admin_configuration.py`。文件标识 `tasks`、`market`、`news` 各映射到设计稿中的固定文件。内部接口 `save_fields(file_id: str, expected_version: str, changes: dict) -> str` 返回新文件版本。

- [ ] 写 `test_stale_form_keeps_external_changes`、`test_invalid_patch_keeps_original_bytes`；覆盖两页同时写、布尔值类型、市场时刻格式、新闻间隔边界、未授权字段和替换失败。验证 API 仅返回表单字段，不返回环境变量或密钥。
- [ ] 执行 `check-admin backend/tests/test_admin_configuration.py -q`，确认新行为尚未具备。
- [ ] 复用现有任务、市场与新闻配置校验；每文件串行，写前比对文件内容哈希，合并允许字段，校验整份配置后在同目录原子替换。返回“已保存”及版本。股票、公告、宏观的只读日程取有效配置，返回来源、频率、时区和触发时刻。
- [ ] 重跑本项测试，确认冲突返回 409、无效字段返回校验错误，其他字段保留，提交。

## 4. 逐项应用调度与有界补采

**文件与接口**　扩展 `backend/app/jobs/scheduler.py`，新增 `backend/app/jobs/scheduler_control.py` 和 `backend/tests/jobs/test_scheduler_control.py`。以 `reconcile(command: ControlCommand) -> ControlReceipt` 接收目标任务、启用代次、文件版本和目标规则，回报接受状态、生效日程及恢复结果。启用代次须区分“关→开→关→开”，不能只用重复出现的文件哈希。

- [ ] 写可控时钟、阻塞工作线程和模拟供应商用例：本轮继续、排队作业失效、其他任务照常执行、仅改时间不补采、首次启用与启动补采相邻仍只执行一次。分别验证无缺口、预算用尽、来源仅提供最新值和恢复失败。
- [ ] 执行 `check-admin backend/tests/jobs/test_scheduler_control.py -q` 验证初始失败。
- [ ] 从现有逐项注册、协调器构造和恢复入口抽出可逐项调用的路径。覆盖启动时未启用、随后才需要构造协调器的任务。关闭时先撤销准入再移除触发器；作业真正取得执行锁时再次核对代次。正在运行的调用保留执行上下文。按原注册约束更新市场五项实际启用的日程。
- [ ] 为八项调度任务分别映射到既有日历、检查点、预算或有限回看策略。只启动目标项恢复，使用已有业务键去重。将实际范围、积压和能力边界写入回执。调度器因管理操作首次启动时协调启动恢复与该命令；常规冷启动仍执行原恢复策略。
- [ ] 重跑本项、`backend/tests/jobs/test_scheduler.py`、`backend/tests/jobs/test_macro_scheduler.py`、`backend/tests/market_data/test_scheduler.py`，全部通过后提交。

## 5. 启动器协调、新闻收尾及回执

**文件与接口**　新增 `backend/app/local_admin/workers.py`、`backend/tests/test_admin_workers.py`，修改 `scripts/start-project.py` 和实际提供 `watch-news` 的 `backend/app/cli/news.py`。把任务 3 的保存结果交给运行方，以 `POST /admin/api/config/{file_id}/apply` 返回文件版本及逐项回执。

- [ ] 用临时文件和受控子进程写 `test_enable_after_empty_start`、`test_news_finishes_round_before_exit`、`test_retry_after_lost_receipt`；覆盖关闭最后一项、收尾时重开、启动失败、父进程结束和重启后恢复，验证不会因预期的子进程退出而终止 API。
- [ ] 执行 `check-admin backend/tests/test_admin_workers.py -q` 验证初始失败。
- [ ] 让启动器持续管理零到两个子进程。建议使用本机专用 SQLite 控制台账，建议路径为 `data/local-admin/control.sqlite3`，实施时在 `.gitignore` 中排除这个本机目录。启动器写命令、子进程领取并回报；保留原有标准输入 EOF 的父进程退出语义。命令带单调代次、目标值和文件版本。先登记待保存意图，再原子写文件，成功后才允许领取；重启按文件版本、进程实例与持久化回执恢复。独立 CLI 和 Compose 不打开台账。首次本机启动自动建台账，业务 PostgreSQL 无迁移。
- [ ] 管理启动的新闻循环复用 `watch-news` 的单轮采集逻辑，在轮次之间处理控制请求并确认实际读取的间隔。区分“唤醒等待”和“中断本轮”，恢复使用原游标及有限回看能力。调度进程在执行工作期间仍能确认新日程。回执丢失先按同一命令查询；对已开始但结果不明的采集，核对业务证据后再决定是否续跑，证据不足则显示待确认。
- [ ] 重跑本项和 `backend/tests/test_configured_startup.py`。验证受控进程在 Windows 可启动、收尾、回收，未启动真实采集。提交。

## 6. 状态、进度与本轮记录

**文件与接口**　新增 `backend/app/local_admin/status.py`、`backend/tests/test_admin_status.py`；管理路由提供 `GET /admin/api/tasks` 和 `GET /admin/api/tasks/{task}/runs/{id}`。从启动器、台账和现有只读状态查询汇总九项任务。

- [ ] 写 `test_stale_running_is_unconfirmed` 和 `test_run_links_require_verified_relationship`。覆盖手动运行与调度运行同名、过期心跳、无历史、新闻单独在线、只有游标无总量、任务编号不匹配。
- [ ] 执行 `check-admin backend/tests/test_admin_status.py -q` 验证初始失败。
- [ ] 对照 `sync-status`、`news-status` 和 ORM 模型读取有效证据。区分期望开关、应用结果、进程活性、运行状态。分页读取近期运行；提供可证实的外键链接（如执行到质量检查、已知批次到该批原件），在表查询入口校验关联范围。业务数据缺少关联时给普通表入口；数量含更新时标为“处理记录”。
- [ ] 重跑本项及任务 2 的数据库测试，在限定范围内翻页并校验归属；通过后提交。

## 7. 串联页面与本机验收

**文件与接口**　完善 `backend/app/local_admin/static/index.html`，新增同目录 `admin.js`、`admin.css`，更新 `docs/local-operations.md`。扩展任务 1 的路由测试并新增 `backend/tests/e2e/test_local_admin_postgres.py`。

- [ ] 写端到端用例串联查看字段及记录、修改文件、进程确认、关闭后收尾及恢复补采；断言页面数据能分别表达“已保存，待应用”“已生效”“仍有积压”。按任务 2 的 PostgreSQL 模式运行新用例，确认预期缺口。
- [ ] 页面串联接口文档、表目录、任务及受限配置表单。任务视图并列展示调度规则、下次触发和最近运行，运行中按需刷新；编辑时保留草稿，应用后按回执刷新。大字段按文本展示，写入浏览器 DOM 时转义库内数据；分别处理加载中、空结果、失败和待确认。
- [ ] 在本机手册补充打开页面、查看数据、配置生效、恢复缺口与本地台账位置。运行本计划涉及的隔离测试、PostgreSQL 端到端用例及 `backend/tests/test_backend_compose_contract.py`，以全部通过为验收依据。
- [ ] 在临时配置、测试数据库和模拟采集器下用浏览器完成设计稿五组验收场景，实看文档链接、宽表分页、长值展开，以及关闭时另一个任务和 API 的连续性。记录实测平台与未验证项后提交。
