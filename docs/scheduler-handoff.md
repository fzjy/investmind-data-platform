# 数据底座常规采集与运行配置交接

本表对应拆分清单 4.1。数据库、迁移、初始化、采集、物化发布、新闻工作进程、任务失败恢复、游标和备份均归数据底座维护。网站消费 HTTP 产品并管理自身用户数据。表中执行时间均为 2026 年北京时间，原始证据保留带偏移的完整时间戳。

## 部署与现场

| 底座单元 | 生命周期与入口 | 核验结果 |
| --- | --- | --- |
| PostgreSQL | `docker-compose.local-db.yml` 只部署数据库。`docker-compose.backend.yml` 的 postgres 使用 PostgreSQL 17 并配置健康检查 | 本机 Docker 在 11 月 14 日仅列出运行中的 `danginvest-postgres`。保留原容器名以便识别本次取证对象 |
| migrate / bootstrap | 前者等数据库健康后运行 `alembic upgrade head`。后者等迁移成功后运行 `python -m app.bootstrap`，检查已发布数据，需要初始化时可采集并发布。显式 `--force` 才强制重建 | 两者均为一次性进程。任务日志中的历史构建不能证明它由 Compose 初始化调用 |
| api | bootstrap 成功后启动 Uvicorn，容器重启策略为 unless-stopped。默认 FastAPI 生命周期会运行新闻采集 | Windows Python 进程中有 4 个命令行匹配 `app.main:app`，可能包含启动器及子进程。没有 API 容器。这一计数不能确定实例所属仓库、生产上线或新闻的具体触发器 |
| scheduler | 仅在显式选择 Compose 的 `scheduler` profile 时启动，依赖 bootstrap 成功。命令为 `python -m app.jobs.scheduler`，重启策略为 unless-stopped | Docker 无 scheduler 容器，Windows Python 无匹配 scheduler、watch-news 或 update-routine 的进程。任务计划程序按项目名和相关入口匹配为 0。数据库心跳及调度检查点均 0 行。此次未启停服务，也未验证其他主机、包装脚本、生产环境或下次触发 |
| 配置与凭据 | `backend/app/config.py` 读取底座根目录的 `.env`、进程环境和支持的文件型密钥；`docker-compose.secrets.yml` 提供文件型密钥覆盖。Dockerfile 将 backend/config、金融指标、资产组合、股票比率目录和新闻包置入镜像 | 只核验版本库声明和本机无敏感值的少量配置。仅修改宿主 `.env` 不会自动把所有变量传入容器。仓库中的 `.env.example` 属模板；真实凭据不在交接表或审计结果中 |

## 调度注册与最后执行

`backend/app/jobs/scheduler.py` 有 11 个候选作业 ID。每个作业最多同时启动 1 个实例并合并错过的触发。证券日任务和公告跟随 `DANG_SYNC_TIMEZONE`、小时和分钟，宏观核心周任务跟随同一时区。五项市场任务跟随 `backend/config/market-data.json` 独立的时间和时区。下表按仓库默认配置，市场五项共用每日上海 19:00、启动回补开启、HTTP 超时 20 秒、同轮重试 0 次。

| 作业 | 注册条件与触发 | 游标、去重与恢复 | 最近证据 |
| --- | --- | --- | --- |
| sync-daily | scheduler 启动即注册，每日上海 19:00，以交易日历判断是否执行 | 按业务日加锁，依据证券池版本、交易日和已完成作业补洞；默认披露回看 7 天、失败告警阈值 3。支持 retry-failures 处理可重试项 | 本库无同名 job_run；另一路径 sync-stock-data 有 15 次，不能据此认定日调度已跑 |
| sync-announcements | 股票常规开关且 announcementIndex.enabled 开启时，同股票时钟；启动也补。当前公告开关 false | 配置池与已发布 K 线范围决定证券与日期窗，按窗口和页续跑；每轮 2 页，回看 7 天，使用公告任务锁 | cninfo 原件批次 2，最近 11-11 13:06:58；对应 2 条窗口游标。缺少定时触发记录 |
| sync-macro | MACRO_SYNC_ENABLED 默认 true，每周一上海 09:00 | 启动时可补最近一次时间槽。槽位、目录版本及哈希核验已完成任务；同进程同槽跳过，CLI/定时共用发布锁。失败或部分完成产生告警 | 96 次均显式标为 CLI，89 成功、4 部分、3 失败。最近 11-08 22:58:10 开始、23:01:52 完成 |
| sync-market-data | ROUTINE_MARKET_ENABLED；默认 false；目录 enabled 为 true | 槽位与目录指纹记录计划/采集/发布，按目标标识记录游标，单轮最多 20 次 | 1 次，10-16 19:13:03 完成；15 个目标游标 |
| sync-market-pulse | ROUTINE_MARKET_PULSE_ENABLED，目录 pulse.enabled；前者默认 false | 固定证券池逐目标游标，单轮最多 60 次 | 1 次，10-17 00:13:53 完成；50 个游标 |
| sync-security-region | ROUTINE_REGION_ENABLED，目录 region.enabled；前者默认 false | 逐证券游标，默认 30 天才须刷新，单轮最多 60 次 | 1 次，10-17 10:11:24 完成；50 个游标 |
| sync-etf-heatmap | ROUTINE_ETF_HEATMAP_ENABLED，目录 etfHeatmap.enabled；前者默认 false | 按 ETF 目标游标、业务日和市场时间槽预检，单轮最多 20 次 | 1 次，10-17 11:13:10 完成；18 个游标 |
| sync-commodities | ROUTINE_COMMODITIES_ENABLED，目录 commodities.enabled；前者默认 false | 按商品请求标识续跑，120 天历史窗，单轮最多 60 次 | 2 次，1 部分、1 完成，最近 10-17 11:53:09 完成；55 个游标 |
| scheduler-heartbeat | 运行中每 60 秒，默认超时 180 秒 | scheduler 启动建立实例，上报状态、下次时间；可回收中断运行 | 心跳 0 行，无最后时刻 |
| snapshot-freshness | 启动立即评估，以后每 300 秒 | 日历推算期望业务日并查看缺口，更新新鲜度和告警；不会轮询供应商 | 无同名执行记录或 scheduler 实例证明 |
| startup-catchup | 每次启动一次，无固定墙钟时刻 | 顺序尝试股票、已启用市场子任务、宏观以及开启的公告。股票补跑上限 10 个业务日，并记录剩余；各子任务有独立检查点 | scheduler_checkpoint 为 0 行，没有可归因于本实例的启动补跑 |

市场五项已有真实作业，但日志的 trigger 缺少 scheduler 标记。补跑和人工调用会复用采集器。单独调整 `DANG_ROUTINE_STOCKS_ENABLED` 不会停掉 scheduler 的 sync-daily，`DANG_ROUTINE_MACRO_ENABLED` 控制手动汇总器的宏观分支，常驻宏观使用 `DANG_MACRO_SYNC_ENABLED`。现有 Compose scheduler 的 21 个显式环境变量中没有七个 ROUTINE 开关。若单独启用五项市场定时，应在底座部署层传入相应开关并核验下一次真实执行。当前没有进行该启用操作。

## 手动日常更新与独立新闻进程

`python -m app.cli.main update-routine` 是一次有界执行。`--dry-run` 用于预检，`--publish` 请求发布，`--only-portfolio` 单独运行组合供给。调用器复用子任务检查点，并按已启用的分支协调供应商限流。下表汇报各分支最近持久化证据；库中无同名 update-routine 运行行，不能从子步骤反推调用者。

| 子步骤 | 运行配置及取数范围 | 最近证据 |
| --- | --- | --- |
| 股票增量 | ROUTINE_STOCKS_ENABLED 默认 true；sync-stock-data 固定 50 股，按单元续跑。本机配置 standard、单轮 512 次、日历前看 31 天，stock_update_as_of 未固定；backfill_as_of 单独设为 2026-09-18 | 15 次中 13 成功、2 失败，最近 10-21 19:56:27 完成；788 个单元游标 |
| 宏观核心补采 | ROUTINE_MACRO_ENABLED 默认 true，catchup-macro 按各来源窗口、请求上限与状态恢复，发布还需检查活跃覆盖。核心宏观周任务另有注册 | 3 次 catchup-macro，最近 10-21 19:54:31 为 partial；20 条系列状态上次成功检查在 10-21 19:54:21 |
| 市场、脉搏、地域、ETF、商品 | 五个开关选择与上表相同的五个采集器。手动参数控制发布 | 各自最后运行和游标见上表 |
| 财报原件 | financial-indicators.json 的 dataSupply 已开，20 年窗、7 天刷新、20 HTTP/轮、20 秒超时，按证券/报表/期间保留证据与进度 | 439 个原件批次，最近 11-12 14:51:52；443 个游标 |
| 业绩预告详情 | 财报、股票及 forecastDetail 开关共同控制；复用财报剩余请求预算，最多 3 次检查，失败退避 15 分钟，retryDays 为 1 | 3 个原件批次与 3 个游标，最近 11-11 13:10:20 |
| 普通公告 | 读取相同 announcementIndex 窗口与分页配置，当前禁用 | 历史窗口与批次见上表；保留原件 |
| 估值与业务分部 | valuationSupply / businessSupply 已开。各自 10 年窗、20 HTTP/轮、20 秒超时，业务分部 7 天刷新。估值使用单独交易日历和证券进度 | 估值证据 101 批，最近 11-11 22:21:42；日历 1 批。主营证据 384 批，最近 11-06 19:05:20 |
| 组合参考数据与分红 | portfolio.json 的 routine.enabled、referenceEnabled、dividendsEnabled 都开启。参考价格每 1 天检查，分红 7 天复查；参考证据、分红证据单轮上限分别 2、20 次 | 参考目录/历史最近 11-04 22:19:31 / 11-05 08:19:26；分红原件 103 批，最近 11-14 12:47:33，分红 routine 游标 62 个，最近更新 13:45:07 |
| 宏观扩展 | 10 个启用组均在调用时检查到期槽。usdcnh、vix、marginBalances、aShareFloatMv 每日上海 19:00，其余六组每周一上海 09:00；组级预算不同、同轮重试均 0。历史专项配置单列，不单独创建常驻任务 | macro-extension-v1 最近 11-07 20:02:46。专项记录包含 householdCredit 最近失败、specialBonds 和 tier1Housing 最近成功等；逐任务见 JSON |
| 快讯 | 正常 API 默认 lifespan 开启后在后台运行；`sync-news` 仅一次，`watch-news` 为显式独立循环。API 循环成功后间隔 300 秒，失败后 30 秒再尝试，watch-news 每轮间隔 300 秒。采集配置每页 20 项、最多 3 页、HTTP 超时 15 秒、重试 1 次、回退 2 秒、重叠 6 小时 | 共 10,797 条已交付队列记录，最近 fetched_at 为 11-14 23:08:13。库内任务锁防止并发写，未确认交付保留待处理。相同记录可能来自 3 种入口，不能由入库时间反推使用了哪种入口 |

## 运维与恢复

- `retry-failures` 是显式重放。全库可重试且未解决的失败有 11 条，另有 45 条不可重试未解决、55 条不可重试已解决。7 个官方成分样本/恢复作业的最新历史行仍为 running 且无完成时间，不能据此判定主机进程仍活跃。本次未重试、改写或删除记录。
- `sync-classification`、独立来源补采、外部导入、目标执行、历史回填、报表重建、发布计划与验证、容量检查和备份恢复均有显式 CLI，92 个实际命令和所属 Python 模块列于证据 cliCommands。单个命令是否请求供应商或发布以对应命令参数为准。watch-news 是其中明确的驻留循环。build-and-publish 最近 10-21 19:57:05 完成，sync-classification 最近 10-14 01:59:36 完成。
- backup-database 显式调用才按到期间隔执行，默认 7 天、至少保留最近 8 份、56 天保留期、1800 秒工具超时。backup-status 读取备份清单，verify-database-backup / prune-database-backups 为独立命令。11 个 scheduler 作业中无备份项，也未核验到匹配的主机计划任务；本轮未读备份内容或独立备份清单，最后实际备份时间未验证。
- 日程、股票池、供应商模式、交易日回看、请求/失败阈值、宏观系列和范围、各类限流、保留时间、发布质量门槛及备份参数归底座。运行值应在底座部署中配置。停掉后续采集时保留来源原件、历史版本、游标和失败证据。对于仍共用调度入口的来源，先核对具体消费与开关，避免只改手动入口而留下常驻采集。

## 重放

在底座环境提供支持的 PostgreSQL 只读连接（DANG_DATABASE_READ_URL），使用已安装后端依赖的解释器，从底座根目录运行。输出是一个新的本地 JSON 文件。

```text
python -I scripts/audit-scheduler-inventory.py --output scheduler-inventory.local.json
```

检查脚本从底座 CLI 注册表读取命令，从 scheduler 语法树读取作业 ID，从版本库读非敏感配置，再在同一 PostgreSQL REPEATABLE READ、READ ONLY 事务汇总日志、原件、失败、游标、心跳、检查点及新闻投递。证据覆盖 72 组作业、68 组游标、97 个来源/数据集组合。它不读取原文、连接值、游标内容或错误正文。部署现场另通过只读的 Docker 运行容器列表、Windows 进程及计划任务匹配盘点，时间为 11 月 14 日晚；不能用历史入库或注册时刻代替持续运行的验收。[逐项证据](research/investmind-scheduler-inventory.json) · [重放脚本](../scripts/audit-scheduler-inventory.py)
