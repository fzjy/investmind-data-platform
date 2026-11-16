# 本机开发与运行手册

在本机运行 Python 服务，用 Docker 保存 PostgreSQL 数据。以下命令从仓库根目录执行。首次使用从第 1 节开始。已初始化过数据库的日常开发从第 3 节开始。

## 1. 首次准备环境和数据库

安装 Python 3.12 或以上版本、Docker 和 Compose。创建一次虚拟环境，此后每次打开新的 Python 终端只需激活它。Windows PowerShell 示例：

```powershell
python -m venv backend/.venv
.\backend\.venv\Scripts\Activate.ps1
```

在 macOS 或 Linux 使用 `python3 -m venv backend/.venv` 和 `source backend/.venv/bin/activate`。若已有项目虚拟环境，直接激活。安装依赖并检查工具入口：

```sh
python -m pip install -e ".[dev]" -e ./packages/news-collector
python -m app.cli.main --help
python -m alembic -c backend/alembic.ini heads
```

这三条命令分别用于安装开发环境、查看命令帮助和查看代码中的最新数据库迁移版本：

- `python -m pip install -e ".[dev]" -e ./packages/news-collector`：在当前虚拟环境中安装后端包及其开发、测试依赖（`[dev]`），以及新闻采集包。`-e` 表示可编辑安装，修改源码后通常无需重新安装。这一步不会启动服务或采集数据。
- `python -m app.cli.main --help`：显示项目命令行工具的帮助，查看可用的采集、更新、状态检查和备份命令；不会执行这些任务。
- `python -m alembic -c backend/alembic.ini heads`：通过 `-c` 指定 Alembic 配置文件，查看代码中最新的数据库结构迁移版本；不会修改数据库，也不表示数据库已经升级到该版本。

其中，`python -m` 表示使用当前 Python 环境运行指定模块。数据库迁移命令的区别如下：

| 完整命令 | 用途 | 是否修改数据库 |
| --- | --- | --- |
| `python -m alembic -c backend/alembic.ini heads` | 查看代码中的最新迁移版本 | 否 |
| `python -m alembic -c backend/alembic.ini current` | 查看数据库当前所处的迁移版本 | 否 |
| `python -m alembic -c backend/alembic.ini upgrade head` | 实际执行迁移，将数据库结构升级到最新版本 | 是，执行前应确认目标数据库 |

项目默认的旧 `DANG_NEWS_CONFIG_PATH=backend/config/news-collector.json` 会自动解析到 `config/news/news-collector.json`，已有部署可以沿用原配置。自定义配置文件仍按指定路径读取。

首次配置时复制 [环境变量模板](../.env.example) 为根目录的 `.env`。填写 `DANG_POSTGRES_DB`、`DANG_POSTGRES_USER` 和 `DANG_POSTGRES_PASSWORD`，再设置 `DANG_DATABASE_URL`，格式为 `postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/DBNAME`。将占位符换成同一组实际值，连接串里的特殊字符需 URL 编码。端口取 `DANG_POSTGRES_PORT`。留空 `DANG_DATABASE_READ_URL` 时读取沿用主连接。填好 `DANG_TUSHARE_TOKEN`，按实际数据来源设置其他凭据。保留已有配置和凭据。

```sh
docker compose -f docker-compose.backend.yml -f docker-compose.local-db.yml up -d --wait postgres
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
```

数据库应为 healthy，最后一条输出的版本应与 `heads` 一致。以后从同一目录用同一 Compose 项目名启动，继续使用原有数据卷。

## 2. 首次准备可供查询的数据

<a id="initial-data"></a>

若复用的数据库已经发布了所需数据，直接进入下一节。空库先选择数据范围，例如开发池 `DANG_SYNC_UNIVERSE=representative-50`。确认数据权限、预算和截止日期后，执行一次初始化。

根配置至少填好数据库、Tushare、股票池与`DANG_BACKFILL_AS_OF`。`backfill-universe`，执行前会输出范围计划，但没有只读dry-run模式；会按配置逐批执行，并在整体就绪后统一构建发布。

```bash
python -m app.cli.main backfill-universe --help
python -m app.cli.main backfill-universe
```

首个命令只看帮助，第二个才执行。失败保留日期、配置、Raw和检查点，修正环境/权限问题后原命令重入；不要清游标。`DANG_BACKFILL_BATCH_SIZE`控制单批证券数，不是每次CLI只跑一批或全任务HTTP总上限。全市场`all`还需`--confirm-full-market`，不在默认开发初始化中。

更细的`backfill`/`build-and-publish`是工程维护工具：先看当前帮助选择`--as-of`、`--symbol`、`--dataset`等；不要在已成功发布后盲目再跑旧日期的全产品构建。它们不代替当前统一日常更新。

## 3. 日常启动、验证和停止

先用第 1 节的 Compose 命令启动原数据库，在已激活虚拟环境的终端启动查询服务：

```sh
python scripts/start-project.py
```

在浏览器打开 `http://127.0.0.1:8000/health`，确认 `database=ready`。再打开 `http://127.0.0.1:8000/api/v1/portfolio`，查看已发布数据的日期和版本。数据库连通而业务接口返回 `DATA_NOT_READY` 时，检查对应产品的构建和发布记录。

本机启动器在同一端口提供[数据底座管理台](http://127.0.0.1:8000/admin/)。若设置了 `DANG_BACKEND_PORT`，使用该端口。任务状态页展示调度规则、进程回执和下一次触发；点任务名可看运行历史和有关联依据的记录。数据表页按 schema 选表，每页 25 条，可以查看字段、翻页和展开长值。接口文档页提供公共接口与字段说明。

接口文档按数据内容分类，支持搜索中文名称、英文字段、来源和表名。每项显示来源、频率、实际获取方式、数据表以及公式输入和口径。点击表名会在新标签页打开当前端口的对应表。保存历期数据、保留每次修订或重算、接口查询历史分别回答“是”“否”或“待核验”，已落库历史的覆盖范围以所列依据和实际记录为准。

目录索引为 `docs/data-catalog.json`，分主题内容在 `docs/data-catalog/`。修改来源、字段或公式后核对目录，可以显式运行 `python -I scripts/export-data-catalog.py` 从已维护的中文语义和现有合同重新整理。该命令只读代码及配置并写文档 JSON。页面参数读取当前 OpenAPI；部分无强响应模型的字段、原始单位和专题留存规则仍标为待核验，后续应依据实际合同补充。使用隔离入口运行 `backend/tests/test_admin_catalog.py` 可检查来源和公式归属、字段中文及文档依据是否齐全。

在“运行配置”内按文件点击“应用”。任务默认关闭；关闭会阻止后续轮次并等本轮完成，恢复会按来源的检查点及限额检查缺口。市场调度时刻由五组任务共用，新闻间隔在下轮读取时生效。任务状态显示保存与生效的区别。本机回执位于 `data/local-admin/control.sqlite3`。只读浏览无需启用采集。结束开发时，在各自终端用 `Ctrl+C` 停止进程，等写入完成后按需停止数据库：

```sh
docker compose -f docker-compose.backend.yml -f docker-compose.local-db.yml stop postgres
```

## 4. 导入文件、发布结果与按需更新

### 导入自己的宏观或内容文件

将 UTF-8 文件放到 `DANG_EXTERNAL_IMPORT_PATH` 指定的本机目录（默认 `backend/imports`）。`recordType=macro` 需要系列、观测日期、发布日期、数值和来源等字段。`recordType=content` 支持新闻、公告和文章，需要标题、来源链接、发布时间和版权状态。完整字段见[输入模型](../backend/app/connectors/external_files/contracts.py)。下面用已有的内容文件举例，每一步成功后再执行下一步。

```sh
python -m app.cli.main validate-import backend/imports/content.jsonl
python -m app.cli.main import-jsonl backend/imports/content.jsonl
python -m app.cli.main rebuild-external-marts --product content
```

校验报错时按行号修正文件。写入后查看导入回执、重建结果及目标产品。新闻可查询 `/api/v1/news` 核对标题和发布时间，宏观文件改用 `--product macro` 并按[宏观接口说明](macro-supply.md)核对系列和观测期。原始数据落库、产品发布和页面可见是三个检查点。

### 从供应商更新已配置的数据组

```bash
python -m app.cli.main update-routine --dry-run
python -m app.cli.main update-routine --publish
```

dry-run读取配置和本地数据库状态，不写业务状态、不发供应商请求；第二条是一次实际更新，不是常驻scheduler。各组根据配置到期、冻结目标和既有检查点执行，条件就绪才发布。组间可有先后依赖，输出包含各组结果与HTTP计数；不能只看一个总状态就断言所有数据完整。

| 数据组 | 配置入口 | 范围/频率边界 |
| --- | --- | --- |
| 证券行情/财报 | 根`DANG_ROUTINE_STOCKS_ENABLED`及`DANG_STOCK_UPDATE_*` | 当前有界入口按representative-50合同；每次默认512HTTP。正常空`DANG_STOCK_UPDATE_AS_OF`按日历选目标 |
| 三表、估值、主营构成 | 根`config/stocks/financial-indicators.json`的供给配置 | 按已发布证券范围动态遍历；字段/单位/历史缺口单列 |
| 组合 | 根 `config/portfolio/portfolio.json` 的 `routine`及证据配置 | 分红/基准独立批次与回看；`--only-portfolio --dry-run`可只读查看这一组 |
| 市场指数、脉搏、地域、ETF、商品 | 根`DANG_ROUTINE_*`开关及`config/market/market-data.json` | 各自日频/缓存周期及预算，市场根开关默认关闭 |
| 基础宏观 | 根`DANG_ROUTINE_MACRO_ENABLED`、周时间及catchup预算 | 当前18项启用、6个有界来源组；每次共享100HTTP上限，并受各组预算限制 |
| 宏观扩展 | `config/macro/macro-extensions.json` | 扩展组自身启用/到期/预算；指定静态来源不等于新期自动发现完成 |

独立运行 CLI 帮助和 dry-run 时不会自动触发上述更新。多日停机时先dry-run，再普通更新；未完冻结批次先恢复，不伪造时间、不换日期绕过失败。已完成同目标正常重入应复用；新的真实目标可能需要新请求。运行时应以报告为准。

### 宏观补采与历史覆盖

- `config/macro/macro-series.json`是当前基础目录。目录20项中18项启用，当前暂停的是`cn.credit.tsf_stock_yoy`与`cn.fx.usdcny`；`cn.cpi.core_yoy`和`cn.rate.dr007`已在启用范围内，不再沿用早期暂停结论。`partial`、`stale`、`fallback`等状态仍保留来源和时效说明，启用不等于历史覆盖完整。

以下是**按需操作参考**，不要整段无条件执行。CLI帮助`--help`不采集；`smoke`会真实访问供应商，需凭据和已确认预算。

| 用途 | 命令 |
| --- | --- |
| FRED最小连通性测试 | `python -m app.cli.main smoke-macro --provider fred` |
| 旧目录周同步入口（先看帮助/预检） | `python -m app.cli.main sync-macro --help` |
| 有界补跑只读计划 | `python -m app.cli.main plan-macro-catchup` |
| 执行有界补跑 | `python -m app.cli.main catchup-macro` |
| 用现有宏观数据重建发布 | `python -m app.cli.main rebuild-external-marts --product macro` |
| 导入后重建内容 | `python -m app.cli.main rebuild-external-marts --product content` |

`DANG_MACRO_CATCHUP_REQUEST_LIMIT=100`是每次物理HTTP共享上限。`activeCoverageCompleted=true`只表示启用范围达到本次目标；catchup本身不自动发布，满足门禁后才执行宏观重建。内容无变化的`no_change`是合法结果。`weekly.completed=false`和`WEEKLY_TOTAL_HTTP_BUDGET_NOT_IMPLEMENTED`是通用周预算未落地的既有边界，不能清空它们，也不能称完整20项通用周任务已完成。`--as-of`/`--force`用于特定维护，不能用来假造日期或跳过失败。

外部JSON/JSONL先`python -m app.cli.main validate-import PATH`，再`import-jsonl PATH`，最后明确重建`macro`或`content`产品。文件放在配置的导入根目录内，内容必须有来源/时间/证券映射及版权政策；导入合同不等于新闻公告采集已实现。历史宏观源、授权、解析范围、过期资料和新期发现缺口见[宏观来源账](macro-supply.md)，不要重跑研究文档中的所有历史日期命令。

FRED声明：This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

使用时遵守[FRED API条款](https://fred.stlouisfed.org/docs/api/terms_of_use.html)。此次手册整理只做受控验证，**真实 smoke未执行**；以前的真实结果只按各自研究证据日期记账。

## 5. 启用定期获取，确认任务实际运行

<a id="scheduled"></a>
<a id="news"></a>

本机管理台可以直接应用任务开关。手动编辑[定时采集配置](../config/scheduled-tasks.json)时，运行 `python scripts/start-project.py --plan` 查看选择结果，然后在页面重新载入并应用，或重启项目。新闻使用 `news-worker`，其余启用的任务共用 `scheduler`。切换前先结束原先手动启动的同类进程。

修改市场调度时间等启动时读取的配置后，在项目终端按 Ctrl+C，再运行 `python scripts/start-project.py`。运行中的新闻工作进程每轮重读[新闻配置](../config/news/news-collector.json)，新的轮询间隔在后续循环生效。日、周任务沿用 `.env` 的时区、日同步小时和分钟、宏观周同步日期和时间，市场各组遵守 `config/market/market-data.json` 的来源配置。日周调度沿用现有 Tushare 凭据和股票池要求。用 `python -m app.cli.main news-status` 或 `python -m app.cli.main sync-status` 查看对应的交付记录、心跳与作业。

`update-routine --publish` 执行一次配置更新。某组能手动更新，并不表示它已注册为定时任务。逐组覆盖见[调度任务清单](scheduler-handoff.md)。停机期间的补跑有上限，先查看检查点和预检报告，再决定是否继续补采。

## 6. 日常维护、备份和升级

### 制作并验证备份

在根`.env`选择一种工具模式，复用`DANG_DATABASE_URL`：

| 配置 | 默认/说明 |
| --- | --- |
| `DANG_BACKUP_TOOL_MODE` | `docker`或`native`，失败不静默切换 |
| `DANG_BACKUP_PATH` | `./backend/backups`，自定义目录也应加入忽略并限制访问 |
| `DANG_BACKUP_NATIVE_BIN` | native模式同主版本`pg_dump`/`pg_restore`所在绝对目录；Windows可用`C:/Program Files/PostgreSQL/17/bin` |
| `DANG_BACKUP_DOCKER_IMAGE` | `postgres:17-alpine`，需预先准备且主版本与源库兼容 |
| `DANG_BACKUP_INTERVAL_DAYS` | `7`，按实际最后成功完成时间计算 |
| `DANG_BACKUP_KEEP_COUNT` | `8`，受保留规则和成功验证锚点共同约束 |
| `DANG_BACKUP_RETENTION_DAYS` | `56`，不表示每天自动删除 |
| `DANG_BACKUP_TIMEOUT_SECONDS` | `1800`，主流程预算 |

Docker模式先确认Docker可用，并在需要时手动`docker pull postgres:17-alpine`准备配置指定镜像；程序不自动拉取。native导出要求本机工具可用；**两种模式的恢复验证都需要Docker隔离容器**。源库loopback通过项目受控转发处理，不改业务PostgreSQL；`verify-full`等不支持组合会明确阻断，不降级TLS绕过。

### 5.2 普通备份与恢复核验

```bash
python -m app.cli.main backup-status
python -m app.cli.main backup-database
python -m app.cli.main verify-database-backup --backup-id BACKUP_ID
```

`backup-status`只读本地清单；备份未到期返回跳过，不用`--force`伪造真实周期。把输出中的实际ID填入验证命令；没有已完成包则先解决备份问题。`archive_valid`表示归档可检查，`restore_verified`才表示已在项目新建隔离容器恢复并核对。verify不接受目标URL，不覆盖业务库。

保留整个`backend/backups/<ID>/`中的`database.dump`、`manifest.json`和生成后的`verification.json`，复制到另一物理盘。归档未加密，SHA-256也不是可信签名，只恢复自己可信的备份，按需另行加密存储。Windows不能通过文本重定向/Out-File转存二进制dump。

清理候选只读查看：`python -m app.cli.main prune-database-backups`；`--apply`才会按规则删除，日常启动不包含它。未知/partial/损坏包不自动删，不清锁抢占。raw/core/derived/mart/ops业务数据没有开发期自动过期策略。

### 将本机备份恢复到另一个新空库

这与隔离verify不同，是部署操作者的明确恢复动作，不是程序自动覆盖源库：

1. 保留旧库及原备份完整包；确认来源可信、校验/恢复证据有效、PostgreSQL主版本兼容。文件来自其他电脑时放在已配置备份目录。`backup-status`按当前数据库主机、端口、库名的来源身份匹配归档；迁机或改名后返回`empty`不表示复制的包不存在或损坏。此时从可信包目录名（应与其manifest中的ID一致）取得真实ID，显式执行`verify-database-backup --backup-id BACKUP_ID`；不要改manifest身份或重新备份空库代替验证。
2. 在目标PostgreSQL创建专用LOGIN角色和**另一个新空库**，例如`danginvest_restored`，不要先运行Alembic。用pgAdmin Restore选择`database.dump`，设置No owner/No privileges；或用下面的同版本工具，密码交互输入。
3. 验证恢复结果和发布记录后，根`.env`写/读URL都指向这个新库；有升级时再用上文 Alembic 命令升级、查看版本。原连接/旧库保留以便回退。
4. 启动项目，检查health和目标页面日期/版本，再进行只读更新计划。恢复路径不重做空库Tushare全历史采集。

示例假定已创建由danginvest拥有的新空库（替换主机、端口、用户名和**真实归档绝对路径**）：

```bash
pg_restore --host=127.0.0.1 --port=5432 --username=danginvest --dbname=danginvest_restored --no-owner --no-acl --exit-on-error --single-transaction /ABSOLUTE/PATH/database.dump
```

Windows使用同一参数并把最后路径改为带引号的实际Windows路径；若工具不在PATH，用PowerShell的`& "C:/Program Files/PostgreSQL/17/bin/pg_restore.exe"`加上述参数。命令**不加`--clean`**；若目标不是空库，停止并确认目标，不能删业务库重试。备份不恢复角色/ACL，需按实际部署创建和授权；这一示例使用数据所有者仅用于本机开发，不冒充生产最小权限部署。

首版只支持限定数据库对象与项目受管schema；自定义对象、扩展、RLS、普通继承、用户rule等遇到阻断以原因码为准，不强行剔除对象。原生macOS/本机native工具、实际新机恢复和生产WAL/PITR、RPO/RTO、异地加密及长期周期需分别验收，这些能力需要在相应部署环境验收。

### 升级与离线回归

升级前先读迁移计划、准备可信备份、保留旧代码与配置；`release-plan`只读计划，`verify-release`是在隔离数据库中实际恢复、升级与验证，不是业务库发布开关：

```bash
python -m app.cli.main release-plan
python -m app.cli.main verify-release --help
```

后者帮助说明`--package`需用实际备份包路径；明确准备好后再执行真实verify。正常迁移入口仍是 Alembic upgrade head，只对已确认目标库执行。升级后的服务需重新启动；不自动降级数据库版本来回滚。

离线回归入口如下。运行器隔离用户 .env，使用受控数据和数据库，默认关闭需要一次性 PostgreSQL 容器的组；`--postgres` 仅用于另行明确启用的一次性容器验收。

```sh
python -I backend/tests/run_isolated.py pytest backend/tests -q
```

底座发布、备份和调度的实际覆盖分别见[接口目录](platform-api.md)、[本机交接](research/investmind-runtime-handoff.md)和[调度交接](scheduler-handoff.md)。

<a id="migrate-to-server"></a>

## 7. 从本机迁移到服务器

1. 记录正在使用的代码版本（`git rev-parse HEAD`）、未提交的部署差异、Alembic 当前版本、关键 API 快照的日期和版本。保存 `news-status` 和 `sync-status` 的输出，列出需要在服务器重建的采集配置、导入目录和密钥。
2. 约定切换时刻，停止本机 `watch-news`、日周调度和其他可能写入的更新或导入任务。等在途写入完成后保持 PostgreSQL 运行。核对最终新闻状态、业务快照和任务检查点，以此作为恢复后的对照。
3. 运行 `python -m app.cli.main backup-database --force`，在停写后生成一份新的最终备份。这里使用 `--force` 是为了绕过普通备份周期。按第 6 节对**本次返回的备份 ID**执行恢复验证。确认 `restore_verified` 后，保留原件并安全传送整个备份目录（含 `database.dump`、`manifest.json`、`verification.json`）。记录转移前各文件的 SHA-256 供接收端核对。
4. 随包交接代码版本、状态记录、运行配置清单和必要的外部文件。供应商密钥通过部署环境单独提供。当前新闻队列与断点随 PostgreSQL 备份迁移，若有尚待迁入的旧 SQLite，再一并交接 `data/news-collector/`。保留本机原库和卷，切换期间保持本机采集停止。
5. 接下来在目标主机继续操作，直接打开[服务器手册：接收本机迁移](server-operations.md#receive-migration)。该章节从服务器环境准备和空库恢复开始，随后核验 API、切换采集，并进入服务器日常运行。
