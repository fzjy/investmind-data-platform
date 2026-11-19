# 财经内容数据集和 InvestMind 适配

`GET /api/v1/datasets/content-documents` 查询当前库存中允许展示的新闻、公告和文章。按需传 `content_type`（`news`、`announcement`、`article`）、`symbol`、`published_from` 和 `published_through`。时间使用带时区的 ISO 8601 日期时间，首尾均包含，查询上限取给定时间与当前时间的较早者。

```http
GET /api/v1/datasets/content-documents?content_type=announcement&symbol=600519.SH&published_from=2026-01-01T00:00:00Z&limit=20
GET /api/v1/datasets/content-documents?content_type=news&limit=3
```

每行给出库存编号 `id`、原始来源 `source`、上游编号 `external_id`、类型、标题、发布时间、原文地址、证券列表和展示权限。`licensed` 与 `public_domain` 可返回保存的摘要、正文，`link_only` 的这两个值为 `null`。其他权限状态及尚未到发布时间的记录不会出现。用户看见的数据受当前权限、修订与留存策略影响。按过去的发布时间筛选只能找到当前仍保留的记录。

结果按发布时间、库存编号倒序。每页默认 50 条，可取 1 到 200 条。携带 `meta.next_cursor` 和原筛选继续，游标为空表示结束。响应 `meta.coverage_scope=stored`。没有匹配项返回空数组。当前内容来源、展示许可和允许的文本集中在[内容查询](../backend/app/content/dataset_query.py)和[文本展示策略](../backend/app/content/content_policy.py)。

| 消费路径 | 共享的事实读取 | 产品专属输出 |
| --- | --- | --- |
| 新闻列表及市场浏览的实时新闻 | 新闻、发布时间、展示范围及稳定顺序 | 来源中文名、上海时间、旧版游标、采集健康状态 |
| 构建市场快照 | 选定时点可展示的内容 | 市场标签、快照日期、展示字段 |
| 个股工作台 | 截止交易日、已发布且关联证券的可展示文档 | 公告类别、交易日映射、预告与快报补充、覆盖窗口 |
| 文件导入 | 合格入库内容与其他来源共享查询 | 文件编号、行号、导入结果及原始凭证留给采集追溯 |
