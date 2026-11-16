# 数据内容目录与接口文档实施计划

> 使用 `superpowers:executing-plans` 在当前会话逐项实施，完成后进行一次独立代码审阅。用户已在本次管理台工作中选择此执行方式。各步骤使用勾选项跟踪。

**目标**　按数据内容浏览完整的底座文档，查看来源、频率、表、明确的历史保存结论和中英文字段及公式，并跳转到真实数据表。

**架构**　版本控制下的结构化目录保存语义与依据，管理子应用拼接当前 OpenAPI 的入参合同。静态页面统一渲染目录和详情，运行状态及可用表清单按需加载，失败时仍可阅读文档。

**技术栈**　沿用 Python、Pydantic、FastAPI、HTML、CSS、原生 JavaScript 及项目隔离测试入口。

**设计依据**　[已确认的设计稿](../specs/2026-11-16-data-catalog-docs-design.md)

## 全局约束

- 本次修改管理台文档与目录元数据，数据内容继续映射到已有独立接口、聚合接口或表。
- 已确认判断先显示“是。”或“否。”，证据不足显示“待核验”。英文标识始终配中文含义。原始值、接口转换值、候选来源和实供来源分别注明。
- 表链接按浏览器当前 origin 构造，使用 `/admin/#tables?s=<schema>&t=<table>`，在新标签页打开。当前 11383 与其他端口都应正常。
- 目录读取和验证使用只读入口。日期、计数和版本来自旧审计时须保留原核验日期。静态文档可以在数据库不可用时阅读。
- 本机路由沿用已有同源和访问校验。元数据作为文本渲染，公共 OpenAPI 与服务器入口的合同延续现状。
- 根目录运行验证命令。PowerShell 可定义 `function check-catalog { & backend/.venv/Scripts/python.exe -I backend/tests/run_isolated.py pytest @args }`。新测试只覆盖目录完整性、合同漂移及真实交互风险。

## 重点审阅

| 输入或故障 | 必须达到的结果 | 负责项 |
| --- | --- | --- |
| 新增供应商字段或公式，历史目录存在旧版同名指标 | 有可验证归属和版本，覆盖检查发现新增缺项，不能将旧公式当成现行公式 | 1、2 |
| 历期留存为是、同日重算覆盖、历史接口查询为否 | 三个独立结论，未知仍为未知 | 1、2、5 |
| OpenAPI 的嵌套引用、可空联合或某条路由已删除 | 参数完整，旧路由显示断链且保留数据说明 | 3 |
| 数据库断开、任务回执缺失或配置尚未应用 | 文档可读，表状态和运行规则如实显示待确认 | 4、5 |
| 快速切换条目、重复字段名、JSON 字段路径、端口变化 | 详情和搜索定位正确，旧响应不覆盖新内容，表链接始终指向当前 origin | 5 |

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `docs/data-catalog.json` | 目录版本、分类、主题文件和来源身份到数据条目的归属清单 |
| `docs/data-catalog/stocks.json`、`markets.json`、`macro.json`、`content-portfolio.json` | 分主题的语义说明、表映射、字段和公式。宏观主题可在本任务内按体量继续拆文件 |
| `backend/app/local_admin/catalog.py` | 目录模型、加载、覆盖及断链校验 |
| `backend/app/local_admin/catalog_contracts.py` | 当前 OpenAPI 的参数解析与目录接口映射 |
| `backend/app/local_admin/catalog_runtime.py` | 仅拼接现有本机任务回执、配置日程和表存在状态 |
| `backend/app/local_admin/routes.py` | 增加管理目录的只读路由 |
| `backend/app/local_admin/static/catalog.js` | 分类、搜索、详情与 URL 状态，不承担采集或配置写入 |
| `backend/app/local_admin/static/admin.js`、`admin.css` | 连接新的文档视图、布局及基础导航 |
| `backend/tests/test_admin_catalog.py`、`test_admin_catalog_contracts.py`、`test_admin_catalog_routes.py` | 覆盖、合同与管理入口的回归验证 |
| `docs/local-operations.md` | 文档入口、历史说明和元数据维护方法 |

## 任务 1　建立目录模型和覆盖清单

**接口**　在 `catalog.py` 定义 `CatalogEntry`、`Catalog` 两个 Pydantic 模型，提供 `load_catalog(project_root: Path) -> Catalog` 和 `validate_catalog(catalog: Catalog, *, project_root: Path, openapi: dict) -> list[dict]`。问题项固定含 `entryId, path, code, message`，便于页面定位。

- [ ] 在 `test_admin_catalog.py` 写 `test_judgment_preserves_unknown_and_separate_history`，断言历史三项分别接受 true、false、null，字段及输入缺中文名时指向具体问题。写 `test_inventory_requires_every_source_and_formula_owner`，断言新增来源字段、宏观系列或公式 ID 未登记归属时校验失败，归属到不存在条目也失败。
- [ ] 执行 `check-catalog backend/tests/test_admin_catalog.py -q`，确认缺少目录模型和覆盖行为。
- [ ] 完成模型。条目包含 `id, name, category, description, status, scope, sources, frequencies, tables, history, derived, fields, calculations, access, evidence`。history 固定三键 `records, revisions, query`，每个判断为 `value: bool | None, explanation, evidence`。字段和计算输入至少含 `id, name, role, type, unit, nullable, description, sourceRefs, tableRefs, derived`；role 使用 `source/storage/response/input`。表引用固定 `schema, table, name, fieldPath`，fieldPath 可以为空。
- [ ] 在目录索引登记分组和主题文件。建立来源身份的归属清单，至少包括 `strategies.FIELD_REGISTRY/STANDARD_API_NAMES/VIP_API_NAMES`、股票更新与专题字段合同、现有 Tushare 字段机器回执、`macro-series.json` 的每个系列、`macro-extensions.json` 各组及发布系列、`FORMULAS`、工作台八卡 `DEFINITIONS`、风险 `RULES`、比率目录及各公共接口。标准与 VIP 归属同类内容；旧版公式按 ID 加版本区分。用代码／配置和历史回执合并盘点，历史身份保留状态及依据。
- [ ] 校验索引到条目的完整性、重复主说明、证据路径存在与字段角色。暂未填完的条目可在开发中报缺项，最终不得用空壳条目满足覆盖。执行本项测试至通过，提交目录模型与已核实的盘点清单。

## 任务 2　核对并填充各类数据内容

**接口**　填充任务 1 的主题文件；各字段 `access` 使用 `kind: independent | aggregate | table`，有 HTTP 能力时含 `method, path, responsePaths, parameterNames, example`。responsePaths 按实际响应数组及对象键标识，每处附中文含义。字段、公式输入和证据可共用引用。

- [ ] 增加 `test_daily_prices_distinguish_stored_and_adjusted`，断言日行情存储未复权四价、工作台返回经过因子转换，三个公历年接口窗口与底层存储范围分别说明。增加 `test_yield_history_formula_and_input_labels`，断言三种股息率均有中文名、公式、单位、输入、窗口和缺失规则，历史三项分别是、否、否。
- [ ] 执行 `check-catalog backend/tests/test_admin_catalog.py -q`，确认语义断言尚未满足。
- [ ] 股票主题核对 `docs/tushare-field-consumers.md`、`screener-api-fields.md`、`workbench-api.md`、`derived-history.md` 及其生产读写代码。合并同义来源，覆盖基础、日行情、估值股本、三表、分红、股东、公告、预告、快报、分部和派生指标。已停采、历史兼容、动态报表字段标明状态，输入 JSON 键映射到实际表；同名旧指标和现行指标说明版本差异。
- [ ] 市场主题核对 `market-supply.md`、目录及各协调器／发布构建器，覆盖指数、ETF、份额、成分、地域、市场聚合、商品与汇率。宏观主题按 `macro-series.json`、扩展配置及 `official-source-fields.md`、`macro-supply.md`、`macro-specialties.md` 建立逐系列或可独立定位的字段说明，并登记利差、转换和企业范围计算依据。
- [ ] 内容与组合主题核对 `news-api.md`、`portfolio-split-lineage.md`、外部文件合同和公共参考目录。单列底座保存的公共数据与网站按用户假设计算的结果。各主题的频率至少区分观测、发布和采集；按源码确认历史策略，存量覆盖引用带日期的审计证据。
- [ ] 对照任务 1 盘点验证每个身份均有实质说明。无证据的未知项写具体缺口，已核实项写明确结论。核对所有已展示字段有中文含义、类型、单位及空值解释，所有派生项有公式和来源。执行本项测试及 `test_macro_documentation_contract.py`、`test_content_import_documentation.py`，通过后提交内容。

## 任务 3　拼接真实接口参数与管理目录 API

**接口**　在 `catalog_contracts.py` 提供 `describe_operation(openapi: dict, method: str, path: str) -> dict`，返回 `available, parameters, requestBody, issues`。在管理子应用提供 `GET /admin/api/catalog` 返回分类与条目摘要，`GET /admin/api/catalog/{entry_id}` 返回详情、参数及问题项。以传入的父应用 `app.openapi()` 为合同来源。

- [ ] 写 `test_nested_openapi_contracts_keep_defaults_required_and_null`，覆盖 path/query/header、模型引用、嵌套对象、数组、anyOf/oneOf、可空、默认值和范围。写 `test_missing_route_keeps_catalog_entry`，断言缺路由时详情保留且问题指向具体 method/path。管理路由用例断言默认公共应用没有新增目录路径、恶意 Host 被拒绝。
- [ ] 执行 `check-catalog backend/tests/test_admin_catalog_contracts.py backend/tests/test_admin_catalog_routes.py -q`，确认缺少接口解析与目录路由。
- [ ] 实现解析，保留嵌套 schema 结构和相对字段路径。目录参数中文解释按完整路径匹配；引用循环按引用路径显示，避免无限展开。复用 `scripts/export-api-requests.py` 已有的合同约定，目录解析覆盖该脚本目前未展开的请求模型。文件加载范围由索引指定，拒绝文件路径穿越；无效条目报具体目录问题。
- [ ] 文档 API 读取目录与当前应用 OpenAPI，不连接供应商或数据库。示例参数按 schema 校验，独立接口为否的内容展示实际聚合入口和响应路径。响应字段用生产读路径及合同确认，保留正文示例的“说明样例”标记。
- [ ] 执行本项测试及 `backend/tests/test_local_admin_routes.py`，比对前后公共 OpenAPI 路径相同，通过后提交。

## 任务 4　附加运行规则与表存在状态

**接口**　在 `catalog_runtime.py` 定义 `runtime_context(manager, table_loader) -> dict`，返回 `tasks, tables, tableState, issues`。通过独立 `GET /admin/api/catalog-runtime` 提供，保持静态目录 API 可单独成功。

- [ ] 在 `test_admin_catalog_routes.py` 写 `test_catalog_readable_when_database_offline`，断言表加载抛数据库异常时目录 200、运行响应标明不可确认；写 `test_disabled_or_pending_task_rules_are_not_reported_active`，核对关闭、保存待应用、有效回执、过期回执的显示依据。
- [ ] 执行本项测试，确认附加运行信息尚未实现。
- [ ] 复用 `status.task_rows(manager)` 与 `tables.list_tables(engine)`。目录 frequencies 引用业务任务 key 或配置路径；只有有运行方证据的规则显示为生效。数据库读取使用原只读入口和超时。表清单未知与表确认不存在分别表达，表加载失败只影响自身字段，任务加载失败亦保留其他内容。
- [ ] 执行本项及 `test_admin_workers.py`，确认运行信息读取不改变开关、配置文件或控制台账，通过后提交。

## 任务 5　按示意页接入分类、搜索与完整详情

**接口**　在 `catalog.js` 导出 `renderCatalog(container: DocumentFragment | HTMLElement, params: URLSearchParams, options: { api: Function, origin: string }) -> Promise<void>`。对接现有 docsPage 并传入浏览器 origin。文档 URL 使用 `#docs?dataset=<id>&field=<id>&q=<text>`，公式另用 `formula=<id>`。

- [ ] 先用临时目录响应与 Playwright 创建有针对性的浏览器验收，覆盖切换条目、搜索重复字段并定位中文标签、刷新／前后退、三种股息率公式、未知条目。添加真实导航用例，在 11383 和随机测试端口分别点表名，断言目标 origin 相同、JSON 路径定位到正确表。未实现页面时应在缺少分类或链接处失败。
- [ ] 统一渲染六类目录和搜索，详情按设计顺序展示。字段长表放进可横向滚动的区域，公式输入按英文标识配中文含义。布尔结论从 value 显示是／否／待核验。显示运行附加信息时，目标与实际规则分开，关闭任务仍显示配置规则。
- [ ] 保留主页面现有异步 generation 的取消保护。新增文档模块内部也按当前 URL 核对后再绘制详情，防止旧请求覆盖。搜索修改使用 replaceState 保留输入焦点，条目和公式选择更新可恢复的 URL，必要时处理 popstate。目录内容和示例用 textContent 渲染，表链接由结构化 schema/table 生成。
- [ ] 添加“目录待更新”、待核验、数据库离线和缺表反馈。动态信息请求失败时主体保持可读。只显示关联的主条目和调用入口，按需展开依据及原文链接。
- [ ] 运行浏览器验收，检查桌面及 480px 窄屏、长表、长字段、输入焦点和同日重算说明。再执行 `node --check backend/app/local_admin/static/catalog.js` 及 `admin.js`，通过后提交。

## 任务 6　完整性检查与交付

**文件**　更新 `docs/local-operations.md`，补充目录元数据文件、更新方法和入口。为目录内容增加可从隔离测试直接运行的完整性校验，复用任务 1 的 validate_catalog。

- [ ] 运行 `check-catalog backend/tests/test_admin_catalog.py backend/tests/test_admin_catalog_contracts.py backend/tests/test_admin_catalog_routes.py backend/tests/test_local_admin_routes.py backend/tests/test_configured_startup.py backend/tests/test_domain_boundaries.py backend/tests/test_backend_compose_contract.py -q`。校验覆盖身份、现有 v1 路由、表模型、中文字段、公式输入、证据链接与目录归属。所有失败先定位并修正。
- [ ] 逐项走读设计稿七组验收。在本机 11383 查看真实目录和表链接，查询仅返回必要元数据；以模拟断库验证降级。记录实际条目数、各类覆盖、待核验内容和依据日期。对样例返回路径与公式至少核对日行情、三表、股息率、宏观和新闻各一项。
- [ ] 更新手册并提交。派发一次整体现有实现与设计对照的独立审阅，重点核对五类故障。对重要发现补充回归验证并修正，再运行受影响检查；其他检查是否扩大取决于修改范围。
- [ ] 交付入口、验证结果和明确的待核验内容，保留已有本机任务开关与原运行方。计划中的验收记录指向实际输出。
