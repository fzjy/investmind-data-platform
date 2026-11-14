# 后端运行与数据维护手册

Database为唯一运行模式。首次依赖安装、建库、平台命令和日常启动见根README的[macOS](../README.md#mac-first-install)与[Windows](../README.md#windows-first-install)。本文集中说明**按需的数据维护/部署操作**，不是开机时顺序执行的脚本。

下列命令在仓库根目录运行。示例以macOS的`npm`表示；Windows PowerShell/CMD换成`npm.cmd`，`node`和`docker`命令不变。不要把占位日期、ID或路径直接用于业务库。

## 1. 配置、入口与数据就绪

- 根`.env`集中运行参数；配置说明见[根README](../README.md#configuration)。生产API需要有效数据库读连接；`DANG_DATABASE_READ_URL`为空复用`DANG_DATABASE_URL`。密钥`*_FILE`优先，配置错误不允许回退明文。
- `/health`执行读连接`select 1`：成功是`status=ok,database=ready`，断库返回503。它不检查所有表和页面快照。
- 迁移用`npm run db:migrate`，查看版本用`npm run db:current`。迁移不会采集数据。已有数据启动用`npm run dev:local`，无需迁移/回填每次重做。
- 只有Raw/Core并不代表页面可用；Provider读取已发布Mart。`DATA_NOT_READY`应查目标产品/版本及构建门禁，不能用Fixture兜底。
- 所有更新遵守[来源总登记](../docs/data-sources.md)，公司数据使用统一批量接口，不人工查单股网站补展示。开发范围`representative-50`不是全市场统计。
- `backend/config/macro-series.json`是当前基础目录。目录20项中18项启用，当前暂停的是`cn.credit.tsf_stock_yoy`与`cn.fx.usdcny`；`cn.cpi.core_yoy`和`cn.rate.dr007`已在启用范围内，不再沿用早期暂停结论。`partial`、`stale`、`fallback`等状态仍保留来源和时效说明，启用不等于历史覆盖完整。

## 2. 数据操作：初始化与更新

<a id="data-operations"></a>

### 2.1 初始证券数据

根配置至少填好数据库、Tushare、股票池与`DANG_BACKFILL_AS_OF`。`backend:backfill`调用`backfill-universe`，执行前会输出范围计划，但没有只读dry-run模式；会按配置逐批执行，并在整体就绪后统一构建发布。

```bash
npm run backend:backfill -- --help
npm run backend:backfill
```

首个命令只看帮助，第二个才执行。失败保留日期、配置、Raw和检查点，修正环境/权限问题后原命令重入；不要清游标。`DANG_BACKFILL_BATCH_SIZE`控制单批证券数，不是每次CLI只跑一批或全任务HTTP总上限。全市场`all`还需`--confirm-full-market`，不在默认开发初始化中。

更细的`backfill`/`build-and-publish`是工程维护工具：先看当前帮助选择`--as-of`、`--symbol`、`--dataset`等；不要在已成功发布后盲目再跑旧日期的全产品构建。它们不代替当前统一日常更新。

### 2.2 日常统一入口

```bash
npm run backend:update -- --dry-run
npm run backend:update -- --publish
```

Windows对应：

```powershell
npm.cmd run backend:update -- --dry-run
npm.cmd run backend:update -- --publish
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

本机启动不会自动触发上述更新。多日停机时先dry-run，再普通更新；未完冻结批次先恢复，不伪造时间、不换日期绕过失败。已完成同目标正常重入应复用；新的真实目标可能需要新请求。运行时应以报告为准。

### 2.3 单组宏观、历史和内容导入

以下是**按需操作参考**，不要整段无条件执行。CLI帮助`--help`不采集；`smoke`会真实访问供应商，需凭据和已确认预算。11.13暂缓范围没有因本文恢复。

| 用途 | macOS命令 | Windows命令 |
| --- | --- | --- |
| FRED最小连通性测试 | `npm run backend:cli -- smoke-macro --provider fred` | `npm.cmd run backend:cli -- smoke-macro --provider fred` |
| 旧目录周同步入口（先看帮助/预检） | `npm run backend:cli -- sync-macro --help` | `npm.cmd run backend:cli -- sync-macro --help` |
| 有界补跑只读计划 | `npm run backend:cli -- plan-macro-catchup` | `npm.cmd run backend:cli -- plan-macro-catchup` |
| 执行有界补跑 | `npm run backend:cli -- catchup-macro` | `npm.cmd run backend:cli -- catchup-macro` |
| 用现有宏观数据重建发布 | `npm run backend:cli -- rebuild-external-marts --product macro` | `npm.cmd run backend:cli -- rebuild-external-marts --product macro` |
| 导入后重建内容 | `npm run backend:cli -- rebuild-external-marts --product content` | `npm.cmd run backend:cli -- rebuild-external-marts --product content` |

`DANG_MACRO_CATCHUP_REQUEST_LIMIT=100`是每次物理HTTP共享上限。`activeCoverageCompleted=true`只表示启用范围达到本次目标；catchup本身不自动发布，满足门禁后才执行宏观重建。内容无变化的`no_change`是合法结果。`weekly.completed=false`和`WEEKLY_TOTAL_HTTP_BUDGET_NOT_IMPLEMENTED`是通用周预算未落地的既有边界，不能清空它们，也不能称完整20项通用周任务已完成。`--as-of`/`--force`用于特定维护，不能用来假造日期或跳过失败。

外部JSON/JSONL先`npm run backend:cli -- validate-import PATH`，再`import-jsonl PATH`，最后明确重建`macro`或`content`产品。文件放在配置的导入根目录内，内容必须有来源/时间/证券映射及版权政策；导入合同不等于新闻公告采集已实现。历史宏观源、授权、解析范围、过期资料和新期发现缺口见[数据总登记](../docs/data-sources.md)，不要重跑研究文档中的所有历史日期命令。

FRED声明：This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

使用时遵守[FRED API条款](https://fred.stlouisfed.org/docs/api/terms_of_use.html)。此次手册整理只做受控验证，**真实 smoke未执行**；以前的真实结果只按各自研究证据日期记账。

## 3. 常驻调度与状态

默认启动网站不启用scheduler。明确需要电脑运行期间自动执行且已核对各组权限/频率后，才单独启动：

```bash
npm run backend:scheduler
npm run backend:cli -- sync-status
```

Windows分别使用`npm.cmd run backend:scheduler`和`npm.cmd run backend:cli -- sync-status`。前者是阻塞常驻进程，状态检查要在另一个终端执行；`Ctrl+C`停止。根配置管理证券日时刻、宏观周时刻和启动补跑，市场组由独立开关和配置进入对应coordinator。

当前scheduler的证券/宏观/市场协调器不等同于`update-routine`全部新增组。组合、财务、业务构成、估值、宏观扩展是否由其他定时入口覆盖须逐组核验，不能看到JSON中的schedule就宣布全部自动采集完成。本文不新建Windows任务、开机服务或任何调度。

停机期间进程不运行；启动补跑是有限恢复，不保证一次覆盖全部积压。`sync-status`反映已有运行/心跳/失败记录。数据库锁仍被活跃任务持有时等待，不删锁抢占。预算、暂停来源、无权限缺口保持原状。

## 4. Compose路径与本机路径

<a id="compose"></a>

### 4.1 仅数据库容器

用于本机前后端的可选组合：

```bash
docker compose -f docker-compose.backend.yml -f docker-compose.local-db.yml up -d --wait postgres
```

只启动postgres；`DANG_POSTGRES_PORT`默认5432，仅绑定127.0.0.1。根数据库URL必须指向这个宿主机端口。基础backend文件本身没有宿主机5432映射。用同一目录/项目名恢复同一卷，已有本机数据库可直接复用，无须容器。

### 4.2 全栈首次初始化或重建

根`.env`填写容器数据库账号、Tushare、`DANG_BOOTSTRAP_SYMBOLS`和可选`DANG_BOOTSTRAP_AS_OF`。符号默认单股600519.SH，不读取代表池自动变成50股。选择一种前端：

基础Compose会直接将账号/密码拼入数据库URL；此路径使用足够长的URL安全字符密码（例如字母、数字、`-`、`_`），不要将含`@`、`/`、`#`等的明文密码直接插入。需要特殊字符或分离凭据时，使用下述secrets覆盖，分别提供原始PostgreSQL密码与正确URL编码的连接文件。更改根配置不会重设旧卷账号。

```bash
npm run compose:database:app
```

开发前端改为`npm run compose:database:dev`；Windows用`npm.cmd`。过程为postgres健康→migrate→bootstrap→api健康→前端。bootstrap需有效Token，即使就绪检查可能跳过；快照未就绪会实际采集和发布。`DANG_BOOTSTRAP_FORCE=true`还要求显式`--force`授权，不属于普通启动。

全栈内部API是`http://api:8000`、数据库是`postgres:5432`；不是本机`.env`URL指向的另一套数据库。前端生产/开发宿主机端口分别由`PORT`/`DEV_PORT`控制，API宿主机端口是`DANG_BACKEND_PORT`。不要再叠加local-db覆盖文件，除非确实要为同一库开放回环端口。

仅前端容器连接宿主机API时，使用`docker-compose.yml`与`FRONTEND_BACKEND_INTERNAL_API_BASE_URL`，不使用全栈覆盖；宿主机API需要按该网络路径可达，不能默认回环绑定在所有环境都可访问。

### 4.3 已创建并初始化的全栈日常启动

保留原项目目录/名称及卷，**只启动现有postgres、api和前端**，不要重新走bootstrap：

```bash
docker compose -f docker-compose.backend.yml -f docker-compose.yml -f docker-compose.fullstack.yml up -d --wait postgres
docker compose -f docker-compose.backend.yml -f docker-compose.yml -f docker-compose.fullstack.yml start api app
```

开发栈把最后的`app`改为`dev`；第二条只用于已经创建的容器，不承诺等待健康。随后用相同三个`-f`参数执行`ps`查看健康，并按根README检查API的`/health`和页面。如果容器不存在，应按首次流程，不要用新卷冒充旧库。首次创建时失败的bootstrap需修正实际原因并续跑首次流程；普通start不会修复缺数据。升级镜像/配置也不能只start旧容器，应先按升级检查再重建。

停止网页服务：

```bash
docker compose -f docker-compose.backend.yml -f docker-compose.yml -f docker-compose.fullstack.yml stop app api
```

开发栈用`dev api`。数据库保留运行；需要停止它时明确单独`stop postgres`。`compose:database:down`会移除该项目容器/网络但默认保留卷；**不要加`-v`**。不删除旧归档、业务库和用户文件来“修复”连接问题。

`docker-compose.secrets.yml`只能显式叠加，固定`.secrets/`文件映射见文件本身；密钥文件不提交。普通Compose只传递文件中列出的环境，不自动透传整份根`.env`。前端容器覆盖dotenv且不带数据库/供应商凭据。scheduler在独立profile中，以上命令都没有启用它。

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
npm run backend:cli -- backup-status
npm run backend:backup
npm run backend:cli -- verify-database-backup --backup-id BACKUP_ID
```

`backup-status`只读本地清单；备份未到期返回跳过，不用`--force`伪造真实周期。把输出中的实际ID填入验证命令；没有已完成包则先解决备份问题。`archive_valid`表示归档可检查，`restore_verified`才表示已在项目新建隔离容器恢复并核对。verify不接受目标URL，不覆盖业务库。

保留整个`backend/backups/<ID>/`中的`database.dump`、`manifest.json`和生成后的`verification.json`，复制到另一物理盘。归档未加密，SHA-256也不是可信签名，只恢复自己可信的备份，按需另行加密存储。Windows不能通过文本重定向/Out-File转存二进制dump。

清理候选只读查看：`npm run backend:cli -- prune-database-backups`；`--apply`才会按规则删除，日常启动不包含它。未知/partial/损坏包不自动删，不清锁抢占。raw/core/derived/mart/ops业务数据没有开发期自动过期策略。

### 5.3 将可信备份用于另一台电脑/新空库

这与隔离verify不同，是部署操作者的明确恢复动作，不是程序自动覆盖源库：

1. 保留旧库及原备份完整包；确认来源可信、校验/恢复证据有效、PostgreSQL主版本兼容。文件来自其他电脑时放在已配置备份目录。`backup-status`按当前数据库主机、端口、库名的来源身份匹配归档；迁机或改名后返回`empty`不表示复制的包不存在或损坏。此时从可信包目录名（应与其manifest中的ID一致）取得真实ID，显式执行`verify-database-backup --backup-id BACKUP_ID`；不要改manifest身份或重新备份空库代替验证。
2. 在目标PostgreSQL创建专用LOGIN角色和**另一个新空库**，例如`danginvest_restored`，不要先运行Alembic。用pgAdmin Restore选择`database.dump`，设置No owner/No privileges；或用下面的同版本工具，密码交互输入。
3. 验证恢复结果和发布记录后，根`.env`写/读URL都指向这个新库；有升级时再运行`db:migrate`、`db:current`。原连接/旧库保留以便回退。
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
npm run backend:cli -- release-plan
npm run backend:cli -- verify-release --help
```

后者帮助说明`--package`需用实际备份包路径；明确准备好后再执行真实verify。正常迁移入口仍是`db:migrate`，只对已确认目标库执行。升级后的服务需重新启动，构建版前端需重建；不自动降级数据库版本来回滚。

开发改动后运行适量检查（不是日常启动步骤）：

```bash
npm run test:architecture
npm run test:python-runner
npm run test:local-runtime
npm run test:backend
npm run typecheck
npm run lint
npm run build
```

Windows把npm换为npm.cmd。后端测试入口会隔离运行配置，默认不连接实际库/供应商；需要一次性PostgreSQL的测试要显式启用，不将业务库URL交给测试。普通测试中的pytest fixture是测试机制，不代表保留了网站Fixture运行模式。

本次11.27验证只包括文档/入口合同、CLI帮助、假配置Compose解析；未执行真实迁移、供应商采集、调度、备份/恢复或Docker镜像部署。真实结果与未验收范围见[整理记录](../docs/research/readme-runbook-2026-11-09.md)，各历史研究/数据缺口见[DEVELOPMENT.md](../DEVELOPMENT.md)与[来源登记](../docs/data-sources.md)。
