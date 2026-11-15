# 长期服务器部署与运行手册

本手册使用 Docker Compose。以下示例在服务器仓库根目录的 Linux/POSIX shell 中执行。新建空库部署依次阅读第 1、2 节；携带本机数据上线请从[接收本机迁移](#receive-migration)进入。两条路径都从第 4 节开始日常运维。

## 1. 准备服务器、配置和持久存储

准备 Docker Engine、Compose、可持久化的磁盘及独立备份位置，检出要部署的代码版本。首次从 `.env.example` 创建 `.env`，已有配置时保留原文件。下面定义一个当前 shell 可用的命令简写。重开终端后在同一目录重新定义它，始终使用同一 Compose 项目名，以便复用原数据卷。

```sh
dc() { docker compose -f docker-compose.backend.yml "$@"; }
dc config --quiet
dc --profile scheduler build
```

| 配置位置 | 首次部署需要决定的内容 |
| --- | --- |
| 根 `.env` 的 `DANG_POSTGRES_DB`、`DANG_POSTGRES_USER`、`DANG_POSTGRES_PASSWORD` | 设置本部署专用的库名、角色和随机密码；基础 Compose 把它们拼入容器内连接串，密码可使用随机生成的 URL 安全字符。既有卷中的角色和密码需要在 PostgreSQL 内管理 |
| `DANG_TUSHARE_TOKEN`、`DANG_TUSHARE_MODE`、`DANG_FRED_API_KEY` | 设置有相应访问权限的供应商凭据。bootstrap 需要 Tushare 凭据；FRED 用于配置了它的宏观任务 |
| `DANG_BACKEND_PORT`、`DANG_BACKEND_CORS_ORIGINS` | API 默认发布宿主机 8000 端口；按消费网站设置来源。基础端口映射绑定宿主机网络接口，按实际访问范围配置服务器防火墙或反向代理 |
| `DANG_EXTERNAL_IMPORT_PATH`、`INVESTMIND_NEWS_STATE_DIRECTORY` | 前者是宿主机导入目录，挂到 scheduler 的 `/imports`；后者保存待兼容迁入的旧新闻状态。数据库卷保存当前业务数据、快讯队列和游标 |
| 根 `config/` | 应用容器只读挂载 `./config:/app/config:ro`。修改任务开关或业务 JSON 后，按[第 6 节](#scheduled)应用变更。`.env` 用于 Compose 插值，只有声明中传入容器的变量才会影响该进程 |

改完配置后再次执行 `dc config --quiet`。部署若叠加 [文件密钥配置](../docker-compose.secrets.yml)，请把该文件作为第二个 `-f` 加到 `dc` 定义，准备声明中的密钥文件并保持所有命令使用相同配置。该覆盖文件给 API 配置独立读连接，当前未给 `news-worker` 声明文件型数据库密钥，启用快讯前需给它配置可用的写连接。完整映射见 [Compose 声明](../docker-compose.backend.yml)。

服务器项目启动器使用宿主机 Python 3.10+。其 `--compose` 模式只依赖标准库。为本次部署填写 [定时任务开关](../config/scheduled-tasks.json)，可先用 `python3 scripts/start-project.py --compose --plan` 预览。若部署使用覆盖文件，启动器相应追加 `--compose-file 文件路径`，并沿用原 Compose 项目名。

## 2. 从空库首次部署

设置 `DANG_BOOTSTRAP_SYMBOLS` 和 `DANG_BOOTSTRAP_AS_OF`（交易日）。默认初始化证券为 `600519.SH`，这决定首次发布范围。确认供应商访问权限后，在仓库根目录执行：

```sh
python3 scripts/start-project.py --compose
dc ps -a
dc logs migrate bootstrap
```

Compose 先等待 PostgreSQL 健康，再执行 `migrate` 建表和 `bootstrap` 初始化，最后启动 API。检查两个一次性服务正常退出、bootstrap 报告 `completed` 或 `skipped`、API 健康。初始发布以外的历史范围按第 5 节补充。现在跳到[日常启停与验收](#daily)。

<a id="receive-migration"></a>

## 3. 接收本机迁移

1. 先完成第 1 节，使用与源端相符的代码版本、PostgreSQL 主版本和数据配置。收到备份包后比对源端记录的三个文件 SHA-256、备份 ID 和 `restore_verified` 回执。本机采集与其他写入在整个切换期间保持停止。备份包留在宿主机受保护的目录。
2. 在**全新的目标部署**中仅启动数据库。首次创建卷时 `POSTGRES_DB` 指定的数据库由 `POSTGRES_USER` 拥有。下面查询输出目标身份与业务表，核对库名、角色，且应没有业务表或迁移版本表。若已有对象，先另备专用新空库并让各服务连接该目标。此阶段保持 API、迁移、初始化与采集服务关闭。

```sh
dc up -d --wait postgres
dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT current_database(), current_user" -c "\dt *.*"'
```

3. 将示例路径替换为实际备份文件。在服务器 shell 从文件读入二进制归档，使用数据库容器自带的同主版本工具，将归档以单个事务恢复到上一步核对的新空库。恢复成功的退出码应为 0。归档恢复业务对象，角色及权限按服务器部署配置。

```sh
dc exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl --exit-on-error --single-transaction' < /absolute/path/to/backup/database.dump
dc run --rm --no-deps migrate alembic current
dc run --rm --no-deps migrate alembic heads
```

4. 对照源端版本和目标代码。如果代码有更新，先按第 7 节准备升级恢复点，再执行 `dc run --rm --no-deps migrate alembic upgrade head`。目标结构已就绪后用 `dc up -d --no-deps api` 仅启动读取服务。这里明确跳过依赖链，以便在恢复验收前保持数据库内容。按第 4 节查询业务快照，对照源端日期与版本。用第 5 节的 `job` 简写运行 `job news-status` 和 `job sync-status`，对照源端最近新闻时间、交付状态和历史作业。此时调度心跳仍应反映交接前的记录。
5. 若有尚待兼容迁入的旧 SQLite，先将其交付到配置的新闻状态目录。确认源端已停写、目标验收通过，再按第 6 节启用所需采集服务并观察首次成功交付及调度心跳。上线前若验收失败，保留本机原库和目标备份以便恢复原运行方；若服务器已产生新写入，先停止目标端采集，盘点新增新闻、游标、批次及发布版本，确定保留或重放范围后再切回。避免两端同时采集。

<a id="daily"></a>

## 4. 日常启停与验收

使用原 Compose 项目和卷。在新终端先定义第 1 节的 `dc`。已有容器按顺序启动，可选采集进程按需启动：

```sh
dc up -d --wait postgres
python3 scripts/start-project.py --compose
dc ps -a
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/v1/portfolio
```

若修改了 `DANG_BACKEND_PORT`，相应替换 URL。第一条查询检查数据库连接，第二条核对已发布产品的日期和版本。`DATA_NOT_READY` 时检查对应产品的构建与发布记录。可用 `dc logs --tail 100 api` 查错误。项目入口按配置恢复所需容器，并停止已关闭的采集服务。仅查询时运行数据库和 API 即可。

停止前等待正在执行的导入或更新完成，先用 `dc --profile scheduler stop news-worker scheduler` 停止已启用的采集服务，再用 `dc stop api postgres` 停止查询和数据库。卷保留数据。`restart: unless-stopped` 使运行中的长期服务受 Docker 重启策略管理；被手动停止的服务按上述步骤启动。

## 5. 导入文件、回填和更新数据

下面使用 scheduler 镜像执行一次性命令，复用它的写连接、供应商凭据和导入挂载。要求数据库已经启动、迁移已完成。在同一个 shell 定义：

```sh
job() { dc --profile scheduler run --rm --no-deps scheduler python -m app.cli.main "$@"; }
```

**文件导入。** 将 UTF-8 JSON 或 JSONL 放在第 1 节指定的宿主机导入目录，默认 `./backend/imports`。`macro` 记录带系列、数值、时点和来源，`content` 记录带标题、链接、发布时间和版权信息，字段见[输入模型](../backend/app/connectors/external_files/contracts.py)。用容器内的文件名操作，以下示例针对一份内容文件，各步成功后继续。

```sh
job validate-import /imports/content.jsonl
job import-jsonl /imports/content.jsonl
job rebuild-external-marts --product content
```

检查校验结果、导入行数和 `affectedProducts`，再核对重建输出。新闻从 `/api/v1/news` 核对标题与时间；宏观文件重建时选择 `--product macro`，并查询 `/api/v1/macro/transmission` 或 `/api/v1/macro/insights` 核对业务时点。重建返回 `no_change` 表示本次没有产品变化。

**初始化更大证券范围。** `job backfill-universe --help` 可查看所需参数。开发池用 `representative-50`。将所需 `DANG_BACKFILL_AS_OF`、股票池和请求预算显式传给一次性容器。例如，先在宿主机导出已选定的实际交易日 `DANG_BACKFILL_AS_OF`，再运行：

```sh
dc --profile scheduler run --rm --no-deps -e DANG_BACKFILL_AS_OF scheduler python -m app.cli.main backfill-universe
```

此命令会实际请求供应商、写入并在数据就绪后发布。失败时保留原范围、日期和检查点，修正原因后续跑。

**日常增量与宏观补采。** 先用 `job update-routine --dry-run` 查看到期组、目标和预算，需要更新时执行 `job update-routine --publish`。证券、财报、估值、主营、分红基准、市场和宏观各组按自身开关执行。单独查看组合可加 `--only-portfolio`。要追赶基础宏观系列时先 `job plan-macro-catchup`，再按计划执行 `job catchup-macro`，达到覆盖条件后 `job rebuild-external-marts --product macro`。核对各组结果与最近发布版本。停机后的冻结批次按原目标恢复。

<a id="scheduled"></a>

## 6. 启用定期采集和调整配置

编辑宿主机的 [定时采集配置](../config/scheduled-tasks.json)，每个任务单独设为 `true` 或 `false`。项目入口需要宿主机 Python 3.10+，Compose 模式只使用标准库。使用文件密钥等覆盖配置时，在每次启动命令末尾追加 `--compose-file docker-compose.secrets.yml` 或对应文件名。自定义项目名沿用 `COMPOSE_PROJECT_NAME`。现有部署首次升级到这个入口时，先执行 `dc --profile scheduler build` 更新工作进程镜像。配置生效后按需检查状态：

```sh
python3 scripts/start-project.py --compose --plan
python3 scripts/start-project.py --compose
dc --profile scheduler ps
dc --profile scheduler logs --tail 100 news-worker scheduler
job news-status
job sync-status
```

| 想调整的任务 | 改哪里 | 怎样验收 |
| --- | --- | --- |
| 快讯 | `config/news/news-collector.json` 的 `enabled`、`interval_seconds`（默认 300）、分页和保留期。持久停采将 `scheduled-tasks.json` 的 `news` 改为 false 后重新应用 | 用 `job news-status` 看上次成功时间、新闻时间和交付状态，确认只有一个工作进程 |
| 股票日更新、基础宏观周更新 | Compose 转发的 `DANG_SYNC_TIMEZONE`、`DANG_SYNC_HOUR`、`DANG_SYNC_MINUTE`、`DANG_MACRO_SYNC_DAY_OF_WEEK` 和 `DANG_MACRO_SYNC_TIME`。默认上海时间每日 19:00 和每周一 09:00 | 用 `job sync-status` 查看心跳与作业；股票日任务还按交易日历判断 |
| 市场指数、脉搏、地域、ETF、商品 | 任务文件中的各组开关及 `config/market/market-data.json` 的范围、预算和时间 | 对照[调度清单](scheduler-handoff.md)查看是否注册、运行及保存检查点 |
| 三表、估值、主营、组合与宏观扩展 | `config/stocks/financial-indicators.json`、`config/portfolio/portfolio.json`、`config/macro/macro-extensions.json`；结合 `job update-routine --dry-run` 看实际启用组 | 已登记的日周调度覆盖其中一部分，其余按需执行一次性更新，核对各组报告 |

变更 Compose 已传入的环境变量后，重新执行对应服务的 `up -d --no-deps` 使其按新配置重建。修改 [定时任务开关](../config/scheduled-tasks.json) 后，重新执行 `python3 scripts/start-project.py --compose`，由启动器增减工作进程并更新任务选择。修改市场调度时间等启动时读取的业务 JSON 后，重启相应的已启用服务，例如 `dc --profile scheduler restart scheduler`。运行中的新闻工作进程每轮重读[新闻配置](../config/news/news-collector.json)，新的轮询间隔在后续循环生效。要用 Compose 未转发的参数，先在部署覆盖文件的相应服务 `environment` 中显式声明并加入 `dc` 命令。停机补跑受预算和检查点限制，查看失败原因后恢复原任务。

## 7. 备份、恢复与升级

**服务器数据库备份。** 基础部署中的应用容器没有宿主机 Docker 工具与备份目录挂载。下面给出直接使用 PostgreSQL 容器工具的方式。目标路径位于宿主机，先准备仅备份用户可写的父目录 `/srv/investmind-backups` 并确认剩余空间。命令为 POSIX shell 二进制重定向，Windows 应使用支持二进制的文件复制方式。

```sh
umask 077
archive_dir=$(mktemp -d /srv/investmind-backups/backup.XXXXXXXX) &&
dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl' > "$archive_dir/database.dump"
```

确认 `pg_dump` 退出码为 0，再用 `dc exec -T postgres pg_restore --list < "$archive_dir/database.dump"` 检查目录，记录文件大小、SHA-256、代码和迁移版本，复制到独立存储。直接导出的文件是 PostgreSQL 归档；项目 CLI 的 `manifest.json`、`verification.json` 由项目备份流程产生。手动归档要在独立的同主版本新空库按第 3 节的 `pg_restore` 步骤实际恢复，并对照数据与快照，才完成恢复验证。保留最近已验证的备份，按部署需要在宿主机定期调用备份并检查结果；当前应用 scheduler 的任务清单没有数据库备份作业。

<a id="upgrade"></a>

**升级。** 先停止采集写入，记录旧代码、服务配置与关键快照，保存可恢复的备份。拉取目标版本后执行 `dc --profile scheduler build`，检查 `dc run --rm --no-deps migrate alembic current` 与 `dc run --rm --no-deps migrate alembic heads`。在维护窗口停止 API，运行 `dc run --rm --no-deps migrate alembic upgrade head`。迁移成功后 `dc up -d --no-deps api`，核对连接、业务日期和版本，再恢复采集。若需要回退，保留失败现场，把升级前备份恢复到另一个新空库，配回对应版本的代码和连接，验证后切换。服务器的真实恢复演练、可接受的数据损失和停机时长需要在目标环境验证。
