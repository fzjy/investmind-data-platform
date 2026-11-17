# 市场快讯的接口与页面用途

底座提供 `GET /api/v1/news`，保留 `/api/data/market-news` 兼容入口。无参返回最新 20 条；limit 为 1–50，下一页将 nextCursor 原样放入 cursor。游标记录上一页末行的发布时间与数据库 ID，按这两列一起倒序，时间相同也能稳定翻页。无效游标返回 400，越界页长返回 422。

| 网站用途与接口字段 | 来源及规则 |
| --- | --- |
| 快讯列表、置顶时间线、外链：id、title、source、sourceUrl、publishedAt、time、market、securities | 新浪财经采集 → 交付队列 ops.news_delivery → core.content_document。数据库 ID 用作稳定行键，published_at 是发布时间，time 转为上海时间；有证券映射时 market 为 A 股。新闻与股票报价各用自己的业务时间 |
| 摘要和正文：copyrightStatus、summary、body | 从 core.content_document 的状态过滤允许展示的记录。link_only 只交付标题/链接等元数据；licensed 和 public_domain 才返回已保存的摘要、正文 |
| 翻页按钮及采集状态：nextCursor、health.status/code、lastSuccessAt、latestPublishedAt、catchingUp、retention | 读当前新闻、ops.job_cursor 的采集与保留状态及 ops.operational_alert 的清理告警。health 是运行状态，不是新闻本身的历史字段 |
| 市场页初始列表：GET /api/v1/market/explorer?news=latest 的 data.marketNews、data.liveNewsPage | 浏览产品按需带当前第一页、游标与健康状态，网站水合后直接复用。手动刷新及翻页才调新闻接口。缺最新页时沿用快照作初始列表并从新闻接口重试。未指定 news 参数时仍返回该浏览版本原有的新闻列表 |

底座的[新闻配置](../config/news/news-collector.json)设置轮询间隔 300 秒、重叠 6 小时、回看 24 小时、12 小时无更新标为滞后。新闻保留 365 天，日志 90 天，清理间隔一天。2026-11-14 的实际清理回执完成，cutoff 为 2025-11-14；这一策略与全项目确认无用后停采保留历史的策略分别登记。当前持续采集由独立 `watch-news` / `news-worker` 进程负责，默认 API 不启动后台采集；部署入口见[本机手册](local-operations.md#news)、[服务器手册](server-operations.md#scheduled)。历史采集和清理结果不能证明当前工作进程仍在运行。

本次在同一个只读事务内遍历 216 页，10,766 条新闻的去重 ID、顺序、标题/链接、发布时间、展示时钟、证券及权限状态均与可交付行一致。样本全部为 link_only。核验时最新页 20 个 ID 全在市场浏览快照的 50 条记录中；保留完整历史浏览产品和独立的最新新闻 API，网站按上表选其一。实时新闻继续更新，复跑时样本数可以变化。[逐页对账摘要](research/investmind-news-contract.json)

股票工作台将同一内容库的证券关联用于提及次数，将巨潮公告元数据用于日线标点。50 只工作台已逐只复核，当前普通公告的历史查核只覆盖金山办公两天，结构化业绩预告及报表披露日各有独立来源。[公告、提及与披露逐项溯源](workbench-announcements.md)

在[定时采集配置](../config/scheduled-tasks.json)中启用 `news` 后，项目启动器会运行一个 `watch-news` 进程，Compose 中对应 `news-worker` 服务。新闻队列、游标和交付状态保存在 PostgreSQL，由项目数据库备份统一保存。
