# InvestMind 数据底座

本项目采集、整理和存储公共金融数据，通过 FastAPI 向 InvestMind 等消费项目提供统一的数据接口。涵盖股票行情与财报、选股和工作台指标、市场与宏观数据、新闻公告，以及组合分析需要的公开输入。

**本仓库不包含前端。** InvestMind 网站属于外部消费项目，通过 HTTP API 读取数据，并独立管理用户资产。文档中的网站页面、`src/`、Next.js 和 npm 命令若用于历史核验，均属于消费项目。

当前处于独立拆分后的离线验收阶段。已有接口和历史核验记录不代表独立服务已部署、持续调度已验收或全市场数据完整；部分证券核验使用 `representative-50` 开发池。

## 1. 数据流程与目录

数据源 → Raw 原始记录 → Core 标准化数据 → Derived 派生指标 → Mart 发布快照 → API。

核心技术为 Python 3.12+、FastAPI、PostgreSQL、SQLAlchemy / Alembic；支持本机 Python 与 Docker Compose 部署。

| 根目录路径 | 内容与用途 |
| --- | --- |
| [`backend/`](backend/) | Python 服务源码，包括 FastAPI、采集任务、调度器、CLI、数据库迁移和测试。 |
| [`config/`](config/) | 业务配置唯一维护目录。定时任务总开关在顶层，股票、组合、市场、宏观和新闻配置按领域分目录。 |
| [`docs/`](docs/) | 本机与服务器运行手册、AI 启动指南、API 规范、字段说明和数据核验记录。 |
| [`packages/`](packages/) | 可复用的 Python 包。目前的 `news-collector/` 包含新闻采集、存储适配和独立测试。 |
| [`scripts/`](scripts/) | 项目启动、API 规范导出、数据审计和跨项目核验脚本。统一启动入口为 `start-project.py`。 |
| [`.vscode/`](.vscode/settings.json) | 共享的编辑器设置，为业务 JSON 关联中文字段说明。 |
| `.git/` | Git 管理的本地仓库目录，保存提交历史、分支引用、索引和仓库设置。 |
| `.tmp/` | 本机测试和核验生成的临时文件，已列入 Git 忽略规则。 |
| [`README.md`](README.md) | 项目首页，提供数据流程、启动方法、配置索引和完整文档目录。 |
| [`pyproject.toml`](pyproject.toml) | Python 数据服务的安装、依赖、测试及业务配置打包规则。 |
| [`LICENSE`](LICENSE) | MIT 许可证，说明代码的使用、修改和分发条件。 |
| `.env` | 当前部署使用的本地环境变量，包括数据库连接、供应商凭据和运行参数。由部署者维护，Git 忽略此文件。 |
| [`.env.example`](.env.example) | 纳入版本管理的环境变量模板，首次部署时复制为 `.env` 并填写实际值。 |
| [`docker-compose.backend.yml`](docker-compose.backend.yml) | 基础 Compose 配置，定义 PostgreSQL、迁移、初始化、查询 API、新闻采集和日周调度服务。 |
| [`docker-compose.local-db.yml`](docker-compose.local-db.yml) | 本机数据库覆盖配置，与基础 Compose 文件配合，将 PostgreSQL 端口绑定到宿主机回环地址，供本机 Python 服务连接。 |
| [`docker-compose.secrets.yml`](docker-compose.secrets.yml) | 文件型密钥覆盖配置，与基础 Compose 文件配合，将部署方提供的数据库和供应商密钥挂载给相关服务。 |
| [`.dockerignore`](.dockerignore) | 指定 Docker 构建时排除的文件，例如本地环境变量、虚拟环境、备份和临时产物。 |
| [`.gitignore`](.gitignore) | 指定 Git 忽略的本地文件，例如实际凭据、备份、缓存和临时目录。 |

## 2. 安装与快速启动

根据运行环境选择手册。两本都包含首次部署、日常启停、数据导入、定期获取、配置生效、备份和升级。

| 我现在要做什么 | 从这里开始 |
| --- | --- |
| 在开发机上使用 Python 和 PostgreSQL 数据库容器 | [本机开发与运行手册](docs/local-operations.md) |
| 在长期服务器上用 Docker Compose 部署 | [服务器部署与运行手册](docs/server-operations.md) |
| 让 AI 从当前环境启动服务并验证结果 | [AI 启动运行指南](docs/ai-startup.md) |
| 把开发机现有数据和采集进度带到服务器 | [本机迁出](docs/local-operations.md#migrate-to-server) → [服务器接收](docs/server-operations.md#receive-migration) |

日常启动会按 [定时采集任务配置](config/scheduled-tasks.json) 选择工作进程。在项目虚拟环境中运行 `python scripts/start-project.py`，服务器运行 `python3 scripts/start-project.py --compose`。首次使用先按对应手册准备数据库和依赖。加 `--plan` 可离线查看选择结果。

业务配置移入根 `config/` 后，日常启动仍使用上述命令。本机首次安装或重新安装后端时，在仓库根目录运行 `python -m pip install -e ".[dev]" -e ./packages/news-collector`。已有 Compose 部署升级时先执行 `docker compose -f docker-compose.backend.yml --profile scheduler build` 重建镜像，再按原方式启动。各应用容器只读挂载宿主机 `./config`。以后修改任务开关，重新运行项目启动器即可。修改其他业务 JSON 时，按[服务器手册第 6 节](docs/server-operations.md#scheduled)让对应服务读取新值。

## 3. 配置文件与相关资料入口

本机运行参数填在根 `.env`，首次配置可复制 [`.env.example`](.env.example)。3.1 列出环境模板、部署和工具配置，3.2–3.3 列出业务规则和数据目录。配置解析源码、镜像构建指令、历史版本及公告证据集中在 3.4。操作步骤见[本机手册](docs/local-operations.md)和[服务器手册](docs/server-operations.md)，底座与网站的配置边界见[配置交接](docs/public-config-handoff.md)。

环境变量、Compose 和 Python 打包配置直接在文件中用 `#` 添加中文注释。Alembic 的 INI 使用英文行内注释，兼容 Windows 系统编码。用 VS Code 打开**整个仓库目录**后，在业务 JSON 中将鼠标停在字段名上即可查看中文用途。使用其他编辑器时，可从下表打开「字段说明」，搜索同名键并阅读 `description`。编辑配置值时写入原 JSON 文件。[编辑器映射](.vscode/settings.json)

### 3.1 环境、部署与开发工具

| 文件 | 用途 |
| --- | --- |
| [`.env.example`](.env.example) | 数据库连接、供应商凭据占位、端口、采集开关、调度、预算、备份等环境变量模板 |
| [`docker-compose.backend.yml`](docker-compose.backend.yml) | 数据库、迁移、初始化、API、新闻工作进程和调度服务的部署声明 |
| [`docker-compose.local-db.yml`](docker-compose.local-db.yml) | 本机使用数据库时的 Compose 覆盖，向宿主机开放数据库端口 |
| [`docker-compose.secrets.yml`](docker-compose.secrets.yml) | 文件型密钥的 Compose 覆盖 |
| [`.dockerignore`](.dockerignore) | Docker 构建上下文排除规则 |
| [`.gitignore`](.gitignore) | 本地配置、备份和生成文件的 Git 忽略规则 |
| [`pyproject.toml`](pyproject.toml) | Python 服务包、运行与开发依赖、测试及包资源配置 |
| [`packages/news-collector/pyproject.toml`](packages/news-collector/pyproject.toml) | 新闻采集包的依赖、打包和命令入口 |
| [`backend/alembic.ini`](backend/alembic.ini) | 数据库迁移工具配置，附[逐项中文说明](backend/README.md#alembic-settings) |

**定时采集开关**统一放在 [scheduled-tasks.json](config/scheduled-tasks.json)。例如只要新闻，就把 `news` 改为 `true`，其他项保留 `false`。项目启动时读取，每项均为 JSON 布尔值。[字段说明](docs/config-fields/scheduled-tasks.schema.json)

| 配置键 | 已启用时执行的任务 |
| --- | --- |
| `news` | 快讯轮询，间隔取新闻配置 |
| `stocks`、`announcements` | 分别控制股票日同步、公告采集及各自的启动补采 |
| `macro` | 基础宏观周同步及已配置的补采 |
| `market`、`marketPulse`、`regions` | 分别控制市场行情、市场脉搏、地域数据 |
| `etfHeatmap`、`commodities` | 分别控制 ETF 热力图、商品数据 |

项目启动入口用这些开关选择定时任务，原有命令仍可单独执行。交易日、宏观周时间、市场与新闻来源的可用性、请求预算继续由各自的环境变量和业务目录控制。首次切换前停止旧的手动采集进程。改完开关，本机重启项目，Compose 重新执行同一启动命令即可应用。

### 3.2 股票、指标与组合

| 文件 | 用途 | 字段说明 |
| --- | --- | --- |
| [`config/stocks/financial-indicators.json`](config/stocks/financial-indicators.json) | 公告、业绩预告、财报、主营、估值的供给策略，以及宏观历史桥接、研究计算与银行评分规则 | [字段说明](docs/config-fields/financial-indicators.schema.json) |
| [`config/stocks/stock-ratio-catalog.json`](config/stocks/stock-ratio-catalog.json) | 股票比率字段目录 | [字段说明](docs/config-fields/stock-ratio-catalog.schema.json) |
| [`config/portfolio/portfolio.json`](config/portfolio/portfolio.json) | 组合公共输入的例行更新、分红及参考数据证据策略 | [字段说明](docs/config-fields/portfolio.schema.json) |
| [`config/stocks/stock-mapping.json`](config/stocks/stock-mapping.json) | 机构与行业分类映射策略 | [字段说明](docs/config-fields/stock-mapping.schema.json) |
| [`config/stocks/universes/representative-50.json`](config/stocks/universes/representative-50.json) | 50 只代表性证券的开发池、版本与覆盖说明 | [字段说明](docs/config-fields/representative-50.schema.json) |

### 3.3 市场、宏观与新闻

| 文件 | 用途 | 字段说明 |
| --- | --- | --- |
| [`config/market/market-data.json`](config/market/market-data.json) | 市场资产目录、市场脉搏、地域、ETF、商品与同步调度策略 | [字段说明](docs/config-fields/market-data.schema.json) |
| [`config/macro/macro-series.json`](config/macro/macro-series.json) | 当前基础宏观系列、来源优先级、转换和同步默认值 | [字段说明](docs/config-fields/macro-series.schema.json) |
| [`config/macro/macro-extensions.json`](config/macro/macro-extensions.json) | 宏观扩展组、来源合同样本和历史补采策略 | [字段说明](docs/config-fields/macro-extensions.schema.json) |
| [`config/macro/enterprise-targets.json`](config/macro/enterprise-targets.json) | 企业样本目标目录、官方来源与证据策略 | [字段说明](docs/config-fields/enterprise-targets.schema.json) |
| [`config/macro/macro-history-sources.json`](config/macro/macro-history-sources.json) | 宏观历史数据来源目录 | [字段说明](docs/config-fields/macro-history-sources.schema.json) |
| [`config/macro/nbs-history-releases.json`](config/macro/nbs-history-releases.json) | 统计局历史发布材料目录 | [字段说明](docs/config-fields/nbs-history-releases.schema.json) |
| [`config/news/news-collector.json`](config/news/news-collector.json) | 新闻采集开关、分页、间隔、重试、历史窗口与保留清理策略 | [字段说明](docs/config-fields/news-collector.schema.json) |

当前基础目录使用 `macro-series.json`，v7、v8 归档也使用同一份[宏观字段说明](docs/config-fields/macro-series.schema.json)。新闻配置的 `enabled` 不会让默认 API 自动开始采集；启停方式见[本机快讯](docs/local-operations.md#news)和[服务器采集](docs/server-operations.md#scheduled)。

### 3.4 源码、构建指令与参考资料

| 用途 | 文件 |
| --- | --- |
| 查看后端如何解析环境变量、默认值和文件型密钥 | [配置解析源码](backend/app/config.py) |
| 查看镜像构建步骤和容器默认启动命令 | [Dockerfile](backend/Dockerfile) |
| 重放历史版本的宏观目录 | [v7 归档](config/macro/archive/macro-series-v7.json)、[v8 归档](config/macro/archive/macro-series-v8.json) |
| 核对比亚迪公司行为事件及其公告来源 | [事件核对记录](docs/research/evidence/portfolio/portfolio-event-resolutions.json)、[2025-033 公告](docs/research/evidence/portfolio/portfolio-evidence/byd-2025-033.pdf)、[2025-047 公告](docs/research/evidence/portfolio/portfolio-evidence/byd-2025-047.pdf) |

## 4. 全部文档入口

以下按用途列出本仓库全部 Markdown 文档，以及 `docs/` 中的 JSON 规范与核验回执。新增、移动或删除文档时同步维护本目录。带日期的研究记录描述当时的证据，不作为当前部署成功或数据实时性的承诺。

### 4.1 运行、开发与交接

- [AI 启动运行指南](docs/ai-startup.md)
- [本机开发与运行手册](docs/local-operations.md)
- [长期服务器部署与运行手册](docs/server-operations.md)
- [手册编排设计](docs/superpowers/specs/2026-11-15-operations-handbooks-design.md) · [实施计划](docs/superpowers/plans/2026-11-15-operations-handbooks.md)
- [Python 数据服务开发说明](backend/README.md)
- [独立新闻采集包](packages/news-collector/README.md)
- [数据底座常规采集与运行配置交接](docs/scheduler-handoff.md)
- [网站展示配置与底座运行配置](docs/public-config-handoff.md)
- [数据审计与网站联测](docs/audit-script-locations.md)
- [离线测试归属](docs/research/platform-test-ownership.md)
- [底座本机配置与开工备份交接](docs/research/investmind-runtime-handoff.md)

### 4.2 API 与字段合同

- [公共数据接口目录](docs/platform-api.md)
- [公共数据 API 请求参数](docs/api-requests.md)
- [从公共字段查 InvestMind 的页面与指标](docs/field-consumers.md)
- [Tushare 原字段到 InvestMind 消费者](docs/tushare-field-consumers.md)
- [官方数据、公开内容及文件导入字段索引](docs/official-source-fields.md)
- [3.7 证券搜索与网站目录](docs/security-search.md)

### 4.3 股票、选股与工作台

- [选股器接口字段与实际供给](docs/screener-api-fields.md)
- [InvestMind 选股器字段用途](docs/screener-field-usage.md)
- [五年估值、机构持股及股东回报的逐股核验](docs/screener-capital-fields.md)
- [选股展示字段的报表与风险核对](docs/screener-display-evidence.md)
- [选股现金与财务质量指标的原件重算](docs/screener-fiscal-cash.md)
- [选股历史数据与浮层口径](docs/screener-history-evidence.md)
- [股票工作台与同业研究接口](docs/workbench-api.md)
- [股票工作台的公告、提及与财务披露](docs/workbench-announcements.md)
- [派生指标的留存、重算和缺口](docs/derived-history.md)
- [Tushare 解禁字段停采与历史查询](docs/share-unlock-field-retirement.md)
- [InvestMind 银行选股数据溯源](docs/research/investmind-bank-lineage.md)
- [股票工作台指标、股东与经营面板核验](docs/research/investmind-workbench-panels.md)
- [工作台三大财报原件核验](docs/research/investmind-workbench-statements.md)

### 4.4 市场、宏观、新闻与组合

- [市场总览与市场浏览的供给核验](docs/market-supply.md)
- [市场历史版本逐版核验](docs/market-history.md)
- [宏观接口、时间序列与修订记录](docs/macro-supply.md)
- [宏观专题与企业样本的重算范围](docs/macro-specialties.md)
- [市场快讯的接口与页面用途](docs/news-api.md)
- [投资组合公开输入与网站私有分析](docs/portfolio-split-lineage.md)

### 4.5 存储与迁移研究

- [重复存储与选股读取复核](docs/research/investmind-storage-review.md)
- [供应商原件正文的重复存储与隔离迁移](docs/research/investmind-raw-body-storage.md)

### 4.6 机器可读规范与核验回执

业务配置的字段说明按文件列在第 3 章，也可以浏览[全部字段说明](docs/config-fields/)。下列 JSON 文件保留原始文件名，便于和对应文档及审计脚本对照；其中历史回执不会随文档整理重写。

- [openapi-v1.json](docs/openapi-v1.json)
- [investmind-bank-lineage.json](docs/research/investmind-bank-lineage.json)
- [investmind-derived-history.json](docs/research/investmind-derived-history.json)
- [investmind-dividend-lineage-observed.json](docs/research/investmind-dividend-lineage-observed.json)
- [investmind-live-snapshot-migration.json](docs/research/investmind-live-snapshot-migration.json)
- [investmind-macro-specialties.json](docs/research/investmind-macro-specialties.json)
- [investmind-macro-supply.json](docs/research/investmind-macro-supply.json)
- [investmind-market-history.json](docs/research/investmind-market-history.json)
- [investmind-market-supply.json](docs/research/investmind-market-supply.json)
- [investmind-metric-replay-04d5657.json](docs/research/investmind-metric-replay-04d5657.json)
- [investmind-metric-replay-aa4ae52.json](docs/research/investmind-metric-replay-aa4ae52.json)
- [investmind-metric-replay-c3ee88d.json](docs/research/investmind-metric-replay-c3ee88d.json)
- [investmind-metric-replay-e8060fc.json](docs/research/investmind-metric-replay-e8060fc.json)
- [investmind-metric-replay.json](docs/research/investmind-metric-replay.json)
- [investmind-news-contract.json](docs/research/investmind-news-contract.json)
- [investmind-non-tushare-field-usage.json](docs/research/investmind-non-tushare-field-usage.json)
- [investmind-platform-storage.json](docs/research/investmind-platform-storage.json)
- [investmind-portfolio-split.json](docs/research/investmind-portfolio-split.json)
- [investmind-raw-body-storage.json](docs/research/investmind-raw-body-storage.json)
- [investmind-scheduler-inventory.json](docs/research/investmind-scheduler-inventory.json)
- [investmind-screener-baseline.json](docs/research/investmind-screener-baseline.json)
- [investmind-screener-capital-fields.json](docs/research/investmind-screener-capital-fields.json)
- [investmind-screener-display.json](docs/research/investmind-screener-display.json)
- [investmind-screener-field-contracts.json](docs/research/investmind-screener-field-contracts.json)
- [investmind-screener-fiscal-cash.json](docs/research/investmind-screener-fiscal-cash.json)
- [investmind-screener-history.json](docs/research/investmind-screener-history.json)
- [investmind-screener-parity.json](docs/research/investmind-screener-parity.json)
- [investmind-security-search.json](docs/research/investmind-security-search.json)
- [investmind-share-unlock-consumers.json](docs/research/investmind-share-unlock-consumers.json)
- [investmind-shared-snapshot-recovery.json](docs/research/investmind-shared-snapshot-recovery.json)
- [investmind-snapshot-storage.json](docs/research/investmind-snapshot-storage.json)
- [investmind-statement-projection.json](docs/research/investmind-statement-projection.json)
- [investmind-stock-research.json](docs/research/investmind-stock-research.json)
- [investmind-tushare-field-consumers.json](docs/research/investmind-tushare-field-consumers.json)
- [investmind-workbench-announcements.json](docs/research/investmind-workbench-announcements.json)
- [investmind-workbench-panels.json](docs/research/investmind-workbench-panels.json)
- [investmind-workbench-quotes.json](docs/research/investmind-workbench-quotes.json)
- [investmind-workbench-risks.json](docs/research/investmind-workbench-risks.json)
- [investmind-workbench-statements.json](docs/research/investmind-workbench-statements.json)

项目许可见 [LICENSE](LICENSE)。
