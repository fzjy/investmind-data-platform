# AI 启动运行指南

收到“启动项目”指令后，从包含 `docker-compose.backend.yml` 的仓库根目录执行。启动项目时按仓库中的定时采集开关恢复所需任务，让查询 API 可用，并用一个业务接口验证已发布数据。先识别部署方式，再执行对应的一节。收到采集需求时再执行第 4 节。

## 1. 识别当前部署

1. 在现有部署的仓库目录运行 `docker compose -f docker-compose.backend.yml ps -a`。沿用原部署的项目名、覆盖文件和数据卷。已有项目 API 容器时走第 3 节。本机使用 Python、Docker 仅运行 PostgreSQL，或本机已有独立 PostgreSQL 时走第 2 节。
2. 检查根目录是否有 `.env`。已有文件直接复用，只核对所需键是否配置，连接串和凭据保留在环境中。首次部署从 `.env.example` 复制，再按对应分支填写。查明配置的 API 端口，默认是 8000。
3. 若目标端口已在监听，核对进程或容器属于本项目，再用第 2 或第 3 节的 `--plan` 对照正在运行的工作进程。配置和进程一致时直接执行第 5 节。需要应用新开关时，本机先停止旧启动器再启动，服务器重新运行 Compose 启动器。端口属于其他服务时，报告占用并使用可用的项目端口。若数据库或服务已存在，优先复用。

## 2. 本机 Python 启动

以下示例用于 Windows PowerShell。复用 `backend/.venv` 的 Python 3.12+ 解释器。在新的开发机上，用 Python 3.12+ 执行 `python -m venv backend/.venv` 创建环境。macOS 或 Linux 的对应解释器路径为 `backend/.venv/bin/python`。

```powershell
$py = (Resolve-Path 'backend/.venv/Scripts/python.exe').Path
& $py --version
& $py -m app.cli.main --help
```

首次安装或缺包时执行 `& $py -m pip install -e ".[dev]" -e ./packages/news-collector`。根 `.env` 中的 `DANG_DATABASE_URL` 指向本次使用的 PostgreSQL，格式为 `postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/DBNAME`。可选的 `DANG_DATABASE_READ_URL` 留空时复用主连接。本机使用项目数据库容器时，连接信息需匹配 `DANG_POSTGRES_USER`、`DANG_POSTGRES_PASSWORD`、`DANG_POSTGRES_PORT`、`DANG_POSTGRES_DB`，执行下列命令并等待 healthy。已有独立数据库则直接检查迁移版本。

```sh
docker compose -f docker-compose.backend.yml -f docker-compose.local-db.yml up -d --wait postgres
```

运行以下两条命令比较数据库当前版本和代码版本。在已选定的新库上执行 `& $py -m alembic -c backend/alembic.ini upgrade head` 建表。已有库落后时按升级流程保留恢复点、执行迁移并复查版本。

```powershell
& $py -m alembic -c backend/alembic.ini current
& $py -m alembic -c backend/alembic.ini heads
```

数据库就绪后，读取仓库的 [定时采集开关](../config/scheduled-tasks.json)，预览并启动项目。启动器会为已启用的任务启动工作进程。新闻立即检查是否到期，日、周任务按配置时间触发，启动补采可能先处理停机期间的缺口。首次从手动命令切换时先停止单独启动的工作进程。使用可持续运行的项目终端并记下进程，退出时用 Ctrl+C 让启动器结束子进程。

```powershell
& $py scripts/start-project.py --plan
& $py scripts/start-project.py
```

继续第 5 节，在另一终端检查 API 和已启用任务的状态。

## 3. 服务器 Compose 启动

下面的 `dc` 是在服务器 POSIX shell 中定义的简写。若现有部署使用密钥覆盖文件或自定义项目名，把原有选项加入函数，所有命令共用同一组参数。

```sh
dc() { docker compose -f docker-compose.backend.yml "$@"; }
dc config --quiet
dc ps -a
```

已有 Compose 部署首次升级到根 `config/` 目录布局时，在服务器手册的[升级流程](server-operations.md#upgrade)中执行 `dc --profile scheduler build`，让 API 和采集镜像都使用新路径。之后的日常启动沿用下面的命令。若 `dc` 引入了覆盖文件，也给 Python 启动器传入 `--compose-file <覆盖文件>`，自定义项目名通过 `COMPOSE_PROJECT_NAME` 传入。

- **已有容器和数据库**。执行 `dc up -d --wait postgres`，再执行 `python3 scripts/start-project.py --compose`。已恢复数据且结构与代码相符、仅缺 API 容器时，使用 `dc up -d --no-deps api`。
- **从空库首次部署**。在根 `.env` 设置数据库名、角色、密码、`DANG_TUSHARE_TOKEN`、初始化证券 `DANG_BOOTSTRAP_SYMBOLS` 和交易日 `DANG_BOOTSTRAP_AS_OF`。此路径会向供应商取数并发布。首次初始化在任务范围内时，执行 `dc --profile scheduler build` 和 `python3 scripts/start-project.py --compose`。通过 `dc ps -a`、`dc logs --tail 50 migrate bootstrap` 检查 migrate、bootstrap 正常退出和 API 启动。若任务是接收已有备份，先走服务器手册的[迁移接收流程](server-operations.md#receive-migration)。

## 4. 用户要求更新或持续采集时

| 需求 | 本机命令（已激活项目虚拟环境） | 服务器命令（使用第 3 节的 dc） |
| --- | --- | --- |
| 查看一次更新计划 | `python -m app.cli.main update-routine --dry-run` | `dc --profile scheduler run --rm --no-deps scheduler python -m app.cli.main update-routine --dry-run` |

持续任务使用 `config/scheduled-tasks.json` 逐项配置。项目启动前通过 `python scripts/start-project.py --plan` 核对选择，本机重启项目或在服务器重新运行 `python3 scripts/start-project.py --compose`。Compose 有密钥等覆盖文件时，启动器应使用相同的 `--compose-file <覆盖文件>` 和项目名。使用 `--no-deps api` 进行迁移验收后，再按已确认的任务配置启动项目。

一次实际更新使用同一条计划命令，将 `--dry-run` 替换为 `--publish`。需要首次填充开发股票池时，设置 Tushare 凭据、股票池及 `DANG_BACKFILL_AS_OF` 后执行 `python -m app.cli.main backfill-universe`。该命令会请求供应商并写库。新闻采集使用一个运行方，复用 PostgreSQL 中的队列和游标。启动所需工作进程后，通过 `news-status` 或 `sync-status` 核对交付状态或调度心跳。本机命令前缀为 `python -m app.cli.main`，服务器前缀为 `dc --profile scheduler run --rm --no-deps scheduler python -m app.cli.main`。

## 5. 验收、交付和停止

等待 API 就绪后发出两个 HTTP 请求，端口随部署调整。PowerShell 示例仅显示必要字段。

```powershell
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/health" | Select-Object status,database
(Invoke-RestMethod "$base/api/v1/portfolio").meta | Select-Object asOf,version
```

服务器可用 `curl -fsS http://127.0.0.1:8000/health` 和 `curl -fsS http://127.0.0.1:8000/api/v1/portfolio` 发出相同请求。

- `/health` 返回 200、`status=ok`、`database=ready`，表示 API 已连通数据库。连接被拒绝时检查端口、Uvicorn 进程或 `dc logs --tail 50 api`。返回 503 时检查实际读连接及目标 PostgreSQL 状态。
- 组合接口返回 200 时，记录 `meta.asOf` 和 `meta.version`。若返回 `DATA_NOT_READY`，分别报告“服务可用”和“组合数据待发布”，按任务需要检查目标产品的初始化及发布记录。
- 向用户报告实际访问地址、已验证的业务日期和版本、已启动或复用的进程，以及尚待处理的错误。保持用户要求运行的服务在线。
- 收到停止指令后，先停止本次启用的采集进程，再停止 API。前台进程用 `Ctrl+C`，本机项目启动器随后会停止其子进程。服务器用 `dc --profile scheduler stop news-worker scheduler` 停止相关采集进程，用 `dc stop api` 停止 API。数据库也需要停机时，在依赖它的服务结束后用原 Compose 项目的 `stop postgres` 保留数据卷。

完整的导入、备份、升级和恢复步骤见[本机开发与运行手册](local-operations.md)和[服务器部署与运行手册](server-operations.md)。
