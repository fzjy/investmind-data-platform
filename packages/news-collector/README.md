# 独立新闻采集包

“独立”指代码解耦。本包提供可复用的新闻采集逻辑，与数据底座后端一起安装；底座负责 PostgreSQL 存储、内容交付和发布。默认 API 只读，持续采集由独立 `watch-news` 进程承担。本仓库不包含前端，也不需要 npm。

[项目首页](../../README.md) · [本机手册](../../docs/local-operations.md#news)、[服务器手册](../../docs/server-operations.md#scheduled) · [新闻 API 合同](../../docs/news-api.md)

## 项目正常使用

从仓库根目录，在已激活的 Python 3.12+ 虚拟环境中安装并完成数据库迁移（连接配置见项目运行手册）：

```sh
python -m pip install -e ".[dev]" -e ./packages/news-collector
python -m alembic -c backend/alembic.ini upgrade head
```

将 [定时采集配置](../../config/scheduled-tasks.json) 中的 `news` 改为 `true`，再从仓库根目录启动项目。本机使用 `python scripts/start-project.py`，服务器使用 `python3 scripts/start-project.py --compose`。两种方式都会启动一个新闻工作进程。单独维护时仍可直接执行 `python -m app.cli.main watch-news`；与项目启动器切换时先结束原来的采集进程。

配置集中`config/news/news-collector.json`，`enabled`控制采集，默认每300秒到期，20条×3页/轮，每请求最多1次重试；超时、退避、重叠/历史目标、失败阈值、保留期和数据库等待边界都在此文件。每轮循环重读配置，修改间隔在后续循环生效；`enabled=false` 时 `watch-news` 退出，重新启用后需重新启动。停止采集进程后停止采集，重启从主库断点继续；API 的启停不控制独立工作进程。

新闻采集运行于独立工作进程，不占用 API 请求处理流程。PostgreSQL 任务锁、事务锁和持久到期时间避免重复采集；来源或数据库暂不可用时保留失败与进度。`Ctrl+C` 停止本机循环，重启复用已提交的断点；突然断电由数据库事务回滚保护。

所有业务新闻、Raw批次、待交付队列、游标和运维告警存入同一个PostgreSQL。`core.content_document`为新闻事实，`raw.external_file/external_record`为规范化批次（news-batch标识不是实际文件），`ops.news_delivery`为待交付/修订队列，`ops.job_cursor/operational_alert`记录状态。队列确认和交付告警恢复同事务；发布失败保留队列。正常更新不写JSONL，不依赖本地SQLite。

项目按新闻发布时间保留`retention_days=365`天；`log_retention_days=90`控制已恢复事件/告警。后台每`cleanup_interval_seconds=86400`秒检查，每批`cleanup_batch_size=500`行、每轮`cleanup_max_batches=20`批，阶段检查点支持中断续跑。清理Core、Raw修订、已交付队列及无引用批次，同时清理当前/历史快照中的新闻副本并重算工作台提及；不会删公告、行情、未交付及关联记录、活跃异常或采集游标。失败独立告警，API/news-status的retention字段报告进度。`enabled=false`同时暂停清理。旧SQLite/JSONL迁移原件和备份归档不自动删改；其保留策略独立管理。

## 旧状态迁移


## 单次预检、状态与更新

```text
python -m app.cli.main sync-news --dry-run
python -m app.cli.main news-status
python -m app.cli.main sync-news --publish
```

`sync-news --dry-run` 只预检，`news-status` 只读状态；`sync-news --publish` 会执行一次有界采集与交付，不会启动常驻进程。单次命令与 `watch-news` 共用锁和到期间隔，不会强制刷新。

## 独立代码与可复用边界


来源为新浪财经7x24结构化接口，以稳定ID向历史翻页；不是有稳定SLA的开放API，东财未核验，不自动替换。只输出标题、来源、时间、原文链接及证券提示，不转载正文/摘要或保存编辑者账号。源码HTML清理集中在适配器。失败不推进游标，达到每轮上限保留nextId；catching_up不表示24小时目标已完整。窗口外修订、来源删除及超出可见历史不保证发现；A→B→A回退视为新修订。初始历史与持续运行按真实证据分别验收。

## 测试

安装后`python -m unittest discover -s packages/news-collector/tests -v`；宿主用项目隔离测试入口。测试不应访问真实HTTP或用户数据库。

采集核心只接受宿主注入的 Store；不提供独立命令或文件存储。宿主保留 watch-news、sync-news、news-status。旧文件迁入通过一次性运维工具完成，正常启动不读取旧文件。
