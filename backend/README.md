# 后端运行与数据维护手册

本仓库提供 Python 数据服务、采集与备份命令。下列命令在数据底座仓库根目录执行，要求 Python 3.12 或以上版本。使用已激活的虚拟环境，在 macOS 与 Windows 上均可执行下列 Python 命令。

```sh
python -m pip install -e "./backend[dev]" -e ./packages/news-collector
python -m app.cli.main --help
python -m alembic -c backend/alembic.ini heads
```

## 1. 配置、入口与数据就绪

- 根`.env`集中运行参数；配置说明见[根 README](../README.md)。生产API需要有效数据库读连接；`DANG_DATABASE_READ_URL`为空复用`DANG_DATABASE_URL`。密钥`*_FILE`优先，配置错误不允许回退明文。
- `/health`执行读连接`select 1`：成功是`status=ok,database=ready`，断库返回503。它不检查所有表和页面快照。
- 迁移用`python -m alembic -c backend/alembic.ini upgrade head`，查看版本用`python -m alembic -c backend/alembic.ini current`。迁移不会采集数据。已有数据库可运行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。API 启动是否运行快讯取决于底座的新闻配置。
- 只有Raw/Core并不代表页面可用；Provider读取已发布Mart。`DATA_NOT_READY`应查目标产品/版本及构建门禁，不能用Fixture兜底。
- 所有更新遵守[底座接口目录](../docs/platform-api.md)，公司数据使用统一批量接口，不人工查单股网站补展示。开发范围`representative-50`不是全市场统计。
- `backend/config/macro-series.json`是当前基础目录。目录20项中18项启用，当前暂停的是`cn.credit.tsf_stock_yoy`与`cn.fx.usdcny`；`cn.cpi.core_yoy`和`cn.rate.dr007`已在启用范围内，不再沿用早期暂停结论。`partial`、`stale`、`fallback`等状态仍保留来源和时效说明，启用不等于历史覆盖完整。

## 2. 数据操作：初始化与更新

<a id="data-operations"></a>

### 2.1 初始证券数据

根配置至少填好数据库、Tushare、股票池与`DANG_BACKFILL_AS_OF`。`backfill-universe`，执行前会输出范围计划，但没有只读dry-run模式；会按配置逐批执行，并在整体就绪后统一构建发布。

```bash
python -m app.cli.main backfill-universe --help
python -m app.cli.main backfill-universe
```

首个命令只看帮助，第二个才执行。失败保留日期、配置、Raw和检查点，修正环境/权限问题后原命令重入；不要清游标。`DANG_BACKFILL_BATCH_SIZE`控制单批证券数，不是每次CLI只跑一批或全任务HTTP总上限。全市场`all`还需`--confirm-full-market`，不在默认开发初始化中。

更细的`backfill`/`build-and-publish`是工程维护工具：先看当前帮助选择`--as-of`、`--symbol`、`--dataset`等；不要在已成功发布后盲目再跑旧日期的全产品构建。它们不代替当前统一日常更新。

### 2.2 日常统一入口

```bash
python -m app.cli.main update-routine --dry-run
python -m app.cli.main update-routine --publish
```

dry-run读取配置和本地数据库状态，不写业务状态、不发供应商请求；第二条是一次实际更新，不是常驻scheduler。各组根据配置到期、冻结目标和既有检查点执行，条件就绪才发布。组间可有先后依赖，输出包含各组结果与HTTP计数；不能只看一个总状态就断言所有数据完整。

| 数据组 | 配置入口 | 范围/频率边界 |
| --- | --- | --- |
| 证券行情/财报 | 根`DANG_ROUTINE_STOCKS_ENABLED`及`DANG_STOCK_UPDATE_*` | 当前有界入口按representative-50合同；每次默认512HTTP。正常空`DANG_STOCK_UPDATE_AS_OF`按日历选目标 |
| 三表、估值、主营构成 | 根`config/financial-indicators.json`的供给配置 | 按已发布证券范围动态遍历；字段/单位/历史缺口单列 |
| 组合 | 根`config/portfolio.json.routine`及证据配置 | 分红/基准独立批次与回看；`--only-portfolio --dry-run`可只读查看这一组 |
| 市场指数、脉搏、地域、ETF、商品 | 根`DANG_ROUTINE_*`开关及`backend/config/market-data.json` | 各自日频/缓存周期及预算，市场根开关默认关闭 |
| 基础宏观 | 根`DANG_ROUTINE_MACRO_ENABLED`、周时间及catchup预算 | 当前18项启用、6个有界来源组；每次共享100HTTP上限，并受各组预算限制 |
| 宏观扩展 | `backend/config/macro-extensions.json` | 扩展组自身启用/到期/预算；指定静态来源不等于新期自动发现完成 |

独立运行 CLI 帮助和 dry-run 时不会自动触发上述更新。多日停机时先dry-run，再普通更新；未完冻结批次先恢复，不伪造时间、不换日期绕过失败。已完成同目标正常重入应复用；新的真实目标可能需要新请求。运行时应以报告为准。

### 2.3 单组宏观、历史和内容导入

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

外部JSON/JSONL先`python -m app.cli.main validate-import PATH`，再`import-jsonl PATH`，最后明确重建`macro`或`content`产品。文件放在配置的导入根目录内，内容必须有来源/时间/证券映射及版权政策；导入合同不等于新闻公告采集已实现。历史宏观源、授权、解析范围、过期资料和新期发现缺口见[宏观来源账](../docs/macro-supply.md)，不要重跑研究文档中的所有历史日期命令。

FRED声明：This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

使用时遵守[FRED API条款](https://fred.stlouisfed.org/docs/api/terms_of_use.html)。此次手册整理只做受控验证，**真实 smoke未执行**；以前的真实结果只按各自研究证据日期记账。

## 3. 常驻调度与状态

默认 Compose 不含 scheduler profile。明确需要电脑运行期间自动执行且已核对各组权限/频率后，才单独启动：

```bash
python -m app.jobs.scheduler
python -m app.cli.main sync-status
```

前者是阻塞常驻进程，状态检查要在另一个终端执行；`Ctrl+C`停止。根配置管理证券日时刻、宏观周时刻和启动补跑，市场组由独立开关和配置进入对应coordinator。

当前scheduler的证券/宏观/市场协调器不等同于`update-routine`全部新增组。组合、财务、业务构成、估值、宏观扩展是否由其他定时入口覆盖须逐组核验，不能看到JSON中的schedule就宣布全部自动采集完成。本文不新建Windows任务、开机服务或任何调度。

停机期间进程不运行；启动补跑是有限恢复，不保证一次覆盖全部积压。`sync-status`反映已有运行/心跳/失败记录。数据库锁仍被活跃任务持有时等待，不删锁抢占。预算、暂停来源、无权限缺口保持原状。

## 4. Compose路径与本机路径

<a id="compose"></a>

### 4.1 仅数据库容器

供本机数据服务使用的数据库容器：

```bash
docker compose -f docker-compose.backend.yml -f docker-compose.local-db.yml up -d --wait postgres
```

只启动postgres；`DANG_POSTGRES_PORT`默认5432，仅绑定127.0.0.1。根数据库URL必须指向这个宿主机端口。基础backend文件本身没有宿主机5432映射。用同一目录/项目名恢复同一卷，已有本机数据库可直接复用，无须容器。

### 4.2 底座容器

从仓库根目录运行 `docker compose -f docker-compose.backend.yml config --quiet` 验证声明。首次实际部署可用 `docker compose -f docker-compose.backend.yml up -d api`，它按 postgres 健康、migrate 成功、bootstrap 成功、api 的顺序启动。在空库执行 bootstrap 会访问供应商，API 可运行快讯采集；单独的 scheduler 必须显式选择对应 profile。容器间数据库使用 postgres:5432，主机访问 API 的端口由 `DANG_BACKEND_PORT` 决定。已创建的服务用 `docker compose -f docker-compose.backend.yml start api` 或 `stop api` 管理。

挂载密钥时显式叠加 `docker-compose.secrets.yml`。修改已运行数据库的凭据时应同时确认现有角色和连接配置。

## 5. 备份、验证与首次恢复

<a id="backup-and-restore"></a>

### 5.1 先准备工具和配置

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

### 5.3 将可信备份用于另一台电脑/新空库

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

首版只支持限定数据库对象与项目受管schema；自定义对象、扩展、RLS、普通继承、用户rule等遇到阻断以原因码为准，不强行剔除对象。原生macOS/本机native工具、实际新机恢复和生产WAL/PITR、RPO/RTO、异地加密及长期周期需分别验收，本文整理没有执行它们。

## 6. 升级和受控验证

<a id="upgrade-and-verification"></a>

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

底座发布、备份和调度的实际覆盖分别见[接口目录](../docs/platform-api.md)、[本机交接](../docs/research/investmind-runtime-handoff.md)和[调度交接](../docs/scheduler-handoff.md)。
