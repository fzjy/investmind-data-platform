# 独立新闻采集包

“独立”指代码解耦。SmartParse正常部署中，本包与后端一起安装，后端启动后自动运行新闻任务，无需另起服务、终端或数据库。

## 项目正常使用

首次安装已包含本包；旧项目更新一次数据库结构：`npm run db:migrate`。正常启动仍是`npm run dev:local`（Windows可用`npm.cmd`），也支持原后端Uvicorn/Compose入口，均由后端生命周期管理新闻后台任务。无需额外运行watch-news。

配置集中`backend/config/news-collector.json`，`enabled`控制采集，默认每300秒到期，20条×3页/轮，每请求最多1次重试；超时、退避、重叠/历史目标、失败阈值、保留期和数据库等待边界都在此文件。运行中循环重读配置，修改间隔最迟在当前等待结束后生效；关闭项目即停止，重启从主库断点继续。只启动Next.js而不启动后端不会采集。

新闻任务在线程中执行，不阻塞API事件循环。PostgreSQL任务锁、事务锁和持久到期时间防止多进程或热重载重复采集；来源或数据库暂不可用时记录失败并退避，不清游标、不关闭网页。停止时通知在途任务结束，有限来源/数据库超时避免无限等待；突然断电由数据库事务回滚保护。

所有业务新闻、Raw批次、待交付队列、游标和运维告警存入同一个PostgreSQL。`core.content_document`为新闻事实，`raw.external_file/external_record`为规范化批次（news-batch标识不是实际文件），`ops.news_delivery`为待交付/修订队列，`ops.job_cursor/operational_alert`记录状态。队列确认和交付告警恢复同事务；发布失败保留队列。正常更新不写JSONL，不依赖本地SQLite。

项目按新闻发布时间保留`retention_days=365`天；`log_retention_days=90`控制已恢复事件/告警。后台每`cleanup_interval_seconds=86400`秒检查，每批`cleanup_batch_size=500`行、每轮`cleanup_max_batches=20`批，阶段检查点支持中断续跑。清理Core、Raw修订、已交付队列及无引用批次，同时清理当前/历史快照中的新闻副本并重算工作台提及；不会删公告、行情、未交付及关联记录、活跃异常或采集游标。失败独立告警，API/news-status的retention字段报告进度。`enabled=false`同时暂停清理。旧SQLite/JSONL迁移原件和备份归档不自动删改；其保留策略独立管理。

## 旧状态迁移

根.env的`DANG_NEWS_STATE_PATH`仅用于定位首版遗留SQLite（默认data/news-collector/state.sqlite），不是新运行存储。首次后台运行在任务锁下只读旧库，主库一批事务导入队列/事件/游标并核对；迁移标记保证重复启动幂等。原SQLite和已有JSONL保留为历史核对材料，不再更新。首次没有旧库后又发现旧文件会明确报告NEWS_LATE_LEGACY_CONFLICT，需要核对来源，不能静默忽略或覆盖已运行的主库。损坏/目标冲突不自动重建。

## 手动诊断（平时不需要）

```text
npm run backend:cli -- sync-news --dry-run
npm run backend:cli -- news-status
npm run backend:cli -- sync-news --publish
```

手动命令与自动任务共用锁和间隔，不会强制刷新。watch-news仅保留诊断兼容入口，正常项目已自动管理，不需要同时运行。

## 独立代码与可复用边界

本包不导入宿主app、不读项目.env。核心依赖Store/Transaction协议；宿主通过DatabaseNewsStore提供PostgreSQL实现。包仍提供标准库SQLiteStore供项目外独立使用和旧格式测试，这不代表SmartParse需要SQLite。SQLite适配器仅按抓取时间清理本地已交付队列/事件，不承担宿主Core/Raw/快照保留策略；项目由news_retention统一处理。单独使用可安装本包后运行`smartparse-news run/status/pending --config <JSON> --state <SQLite>`。

来源为新浪财经7x24结构化接口，以稳定ID向历史翻页；不是有稳定SLA的开放API，东财未核验，不自动替换。只输出标题、来源、时间、原文链接及证券提示，不转载正文/摘要或保存编辑者账号。源码HTML清理集中在适配器。失败不推进游标，达到每轮上限保留nextId；catching_up不表示24小时目标已完整。窗口外修订、来源删除及超出可见历史不保证发现；A→B→A回退视为新修订。初始历史与持续运行按真实证据分别验收。

## 测试

安装后`python -m unittest discover -s packages/news-collector/tests -v`；宿主用项目隔离测试入口。测试不应访问真实HTTP或用户数据库。
