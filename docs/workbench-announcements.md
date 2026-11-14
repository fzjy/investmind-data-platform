# 股票工作台的公告、提及与财务披露

InvestMind 从 `GET /api/v1/stocks/{symbol}` 的一次工作台响应读取行情栏和日线事件。2026-11-14 在同一个只读数据库事务中逐只核对了选股池的 50 个工作台。各工作台价格业务日均为 2026-10-21，内容可以在此后单独更新。回执按证券列出工作台发布版本、原文档 ID、供应商原件 ID、披露日历行号和已查窗口。[逐证券回执](research/investmind-workbench-announcements.json)

| 项目页面与字段 | 底座来源、交付口径和实际覆盖 |
| --- | --- |
| 行情栏 `quote.mentions/latestMention` | 对允许展示且 `core.content_document.securities` 明确包含本证券的文档计数，最新时间取 `published_at`。36 只有提及，14 只没有。224 篇去重新浪快讯贡献 239 次证券关联，8 份巨潮公告贡献另外 8 次。每个关联都对到了 Raw 的证券、标题、外链和发布时间。当前各股最后提及后，库中没有更新的已关联可展示文档。页面没有保存明确的扫描截止时点。 |
| 日线标点 `dailyChart.announcements[]` | 巨潮 `information/topSearch/query` 唯一识别证券，`hisAnnouncement/query` 按股、日期窗口与页码读取；`raw.source_payload` 冻结页，完成全窗口后以公告 ID 写入 `core.content_document`。图上逐件取 `id/title/label/sourceUrl/publishedAt/securities/copyrightStatus`，按标题和报告类型分类。巨潮公开列表只核准上海披露日期，`disclosureTime` 留空。标点日依赖已发布个股日 K、交易日历与停牌记录，保留 `placementStatus/placementReason`。本次 8 份均已定位、均只供元数据与原文链接。 |
| 公告查核范围 `dailyChart.announcementCoverage.windows[]` | `ops.job_cursor` 的任务 `workbench-cninfo-history-v1` 保存每股每窗状态，`completed` 且 `count=0` 才可显示“已查，无公告”。巨潮适配覆盖池内 26 只沪股和 21 只深股，3 只北交所证券不在适配范围。已发布日 K 与最多五年搜寻窗口相交，共 51,357 个可查证券日；仅金山办公 2 个证券日有完成记录，其他 51,355 个未核查。2026-07-29 窗口有 2 份，2026-08-20 有 6 份，原件分别为 3708、3707。两窗口均经末页标记、唯一 ID 和供应商返回总数核实。登记的配置 `announcementIndex.enabled=false`，每次上限 2 页、回看 7 天。 |
| 报告类公告浮层 `announcements[].reportPeriod/financialReport.metrics` | 标题识别出的正式报告按证券、报告期、上海披露日匹配 `core.income_statement`、`core.financial_indicator`。收入和归母利润优先单季，必要时按相邻累计期和相同公司类型相减；EPS、ROE 取单季 `q_eps/q_roe`。环比用差额除以上季绝对值，ROE 另列百分点。金山办公半年报与摘要为两个独立文档，其公告 ID 为 1225482678、1225482695，共计 8 个本期单元格，6 个有值。2 个单季 ROE 因同日版本冲突保留为空。页面金额由元除以一万显示为万元。 |
| 业绩预告 `dailyChart.forecastEvents[].forecast` | Tushare `forecast` 原件到 `core.financial_forecast`，使用证券、实际披露日和期末核对类型、净利润同比区间、净利上下限（万元）、摘要、变动原因。本池有 110 条，110 条均逐项对到 Raw，其中 99 条在图内已定位、3 条在图内待定位、8 条在图外。只有同证券、同披露日、同报告期的公告和预告各有唯一记录时，网页才把预测数值附到原文标点。本次仅 1 对。 |
| 财报面板、报表更新作业 | 三表的 `reportMetadata.announcementDate/originalAnnouncementDate/sourceIds` 来自所选的供应商财报版本。另有 Tushare `disclosure_date` 写入 `core.disclosure_schedule`，按 `actual_date` 生成定期财报增量任务。当前 50 只有 598 个证券期末的日历行，其公告日、期末、预披露日、实际日和变更日都能在冻结原件定位。日历按证券和报告期更新，原件保留抓取历史。普通公告数量按文档 ID 另计。[三表逐期核验](research/investmind-workbench-statements.md) |
| 市场快讯和文章 | `/api/v1/news` 只交付可展示的 `content_type=news`。本次库存为 10,791 条新浪快讯及 8 条巨潮公告，全部为 `link_only`。导入合同接受 `article`，库中该类型为 0，当前未发现交付文章正文的页面或 v1 接口。页脚的 GitHub 入口是固定外链。 |

当前池保有 1,592 个 `mart.stock_workbench_snapshot` 历史版本，每股 27 至 52 个。上述逐件对账针对当前发布版本。历史版本数量只记录留存状况；完整证券公告历史的可证范围以两段已查窗口为准。审计不接供应商、不采集、不发布快照。[只读复核脚本](../scripts/audit-workbench-announcements.py)
