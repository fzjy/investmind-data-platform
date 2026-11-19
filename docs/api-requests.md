# 公共数据 API 请求参数

由生产 FastAPI 路由离线导出。各接口旁的供给文档记录返回字段、单位、原字段、数据库位置、更新节奏和历史边界。[机器可读请求规范](openapi-v1.json) · [字段消费者](field-consumers.md)

## 数据获取状态

`GET /api/v1/data-status` · [返回字段与供给说明](platform-api.md#数据获取状态)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `entryId` | 否 | 数组（string） / null | 见请求模型 |
| query | `itemId` | 否 | 数组（string） / null | 见请求模型 |
| query | `subjectKey` | 否 | 数组（string） / null | 见请求模型 |
| query | `subjectKind` | 否 | string / null | 见请求模型 |
| query | `scopeKind` | 否 | string | default="routine" |
| query | `view` | 否 | string | default="stored" |
| query | `product` | 否 | string / null | 见请求模型 |
| query | `version` | 否 | string / null | 见请求模型 |
| query | `period` | 否 | string / null | 见请求模型 |
| query | `limit` | 否 | string | default="25" |
| query | `cursor` | 否 | string / null | 见请求模型 |

## 市场快讯

`GET /api/v1/news` · [返回字段与供给说明](news-api.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `cursor` | 否 | string / null | maxLength=300 |
| query | `limit` | 否 | integer | default=20, minimum=1, maximum=50 |

## 市场总览

`GET /api/v1/market/overview` · [返回字段与供给说明](market-supply.md)

无需请求参数。

## 市场浏览、商品及热力图

`GET /api/v1/market/explorer` · [返回字段与供给说明](market-supply.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `news` | 否 | snapshot / latest | default="snapshot" |

## 宏观传导

`GET /api/v1/macro/transmission` · [返回字段与供给说明](macro-supply.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `version` | 否 | string / null | minLength=1, maxLength=64 |
| query | `view` | 否 | full / initial / scope | default="full" |

## 宏观观察、企业范围及采集状态

`GET /api/v1/macro/insights` · [返回字段与供给说明](macro-specialties.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `version` | 否 | string / null | minLength=1, maxLength=64 |

## 组合公共输入

`GET /api/v1/portfolio` · [返回字段与供给说明](portfolio-split-lineage.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| header | `if-none-match` | 否 | string / null | 见请求模型 |

## 证券研究与配对

`POST /api/v1/stocks/research` · [返回字段与供给说明](workbench-api.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| body | `symbol` | 是 | string | 见请求模型 |
| body | `peerSymbol` | 否 | string / null | 见请求模型 |

## 股票筛选、排序与分页

`POST /api/v1/stocks/screener` · [返回字段与供给说明](screener-api-fields.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| body | `filters` | 是 | ScreenerFilters | 见请求模型 |
| body | `bank` | 否 | BankRequest / null | 见请求模型 |
| body | `sortRules` | 是 | 数组（SortRule） | 见请求模型 |
| body | `page` | 是 | integer | minimum=1.0 |
| body | `pageSize` | 是 | integer | minimum=1.0, maximum=200.0 |
| body | `sortMode` | 否 | advanced / quick | default="advanced" |

## 个股工作台

`GET /api/v1/stocks/{symbol}` · [返回字段与供给说明](workbench-api.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| path | `symbol` | 是 | string | 见请求模型 |

## 股票未复权日行情

`GET /api/v1/datasets/stock-daily` · [返回字段与供给说明](stock-datasets.md)

查询单日时传 `trade_date`，可加 `symbol`。查询单股区间时同时传 `symbol`、`start_date` 和 `end_date`，包含首尾两日。日期使用 `YYYY-MM-DD`。下一页沿用相同条件，将 `meta.next_cursor` 传给 `cursor`。

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `symbol` | 否 | string / null | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `trade_date` | 否 | string / null | format="date" |
| query | `start_date` | 否 | string / null | format="date" |
| query | `end_date` | 否 | string / null | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |

## 股票复权因子

`GET /api/v1/datasets/stock-adjustment` · [返回字段与供给说明](stock-datasets.md)

查询单日时传 `trade_date`，可加 `symbol`。查询单股区间时同时传 `symbol`、`start_date` 和 `end_date`，包含首尾两日。日期使用 `YYYY-MM-DD`。下一页沿用相同条件，将 `meta.next_cursor` 传给 `cursor`。

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `symbol` | 否 | string / null | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `trade_date` | 否 | string / null | format="date" |
| query | `start_date` | 否 | string / null | format="date" |
| query | `end_date` | 否 | string / null | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |

## 股票每日估值与股本

`GET /api/v1/datasets/stock-daily-basic` · [返回字段与供给说明](stock-datasets.md)

查询单日时传 `trade_date`，可加 `symbol`。查询单股区间时同时传 `symbol`、`start_date` 和 `end_date`，包含首尾两日。日期使用 `YYYY-MM-DD`。下一页沿用相同条件，将 `meta.next_cursor` 传给 `cursor`。

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `symbol` | 否 | string / null | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `trade_date` | 否 | string / null | format="date" |
| query | `start_date` | 否 | string / null | format="date" |
| query | `end_date` | 否 | string / null | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |

## 证券主数据与曾用名

`GET /api/v1/datasets/stock-securities` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `symbol` | 否 | string / null | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |

## 交易所日历

`GET /api/v1/datasets/stock-trade-calendar` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `exchange` | 是 | string | minLength=1, maxLength=16, pattern="^[A-Z0-9]+$" |

## 分类成员与生效期

`GET /api/v1/datasets/stock-classification-members` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `symbol` | 是 | string | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |

## 财务报表及已存版本

`GET /api/v1/datasets/stock-financial-reports` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `symbol` | 是 | string | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `statement` | 是 | income / balance / cashflow / indicator | 见请求模型 |

## 分红事件与时点证据

`GET /api/v1/datasets/stock-dividends` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `symbol` | 是 | string | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `as_of` | 否 | string / null | format="date" |

## 财报披露日程

`GET /api/v1/datasets/stock-disclosures` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `symbol` | 是 | string | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |

## 公司公告事件

`GET /api/v1/datasets/stock-company-events` · [返回字段与供给说明](foundation-stock.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `symbol` | 是 | string | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `kind` | 是 | forecast / express / repurchase / unlock | 见请求模型 |

## 指数和 ETF 日行情

`GET /api/v1/datasets/market-daily` · [返回字段与供给说明](foundation-markets-macro.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |
| query | `asset_code` | 是 | string | pattern="^[A-Z0-9][A-Z0-9.]{0,31}$" |
| query | `asset_type` | 是 | indices / etfs | 见请求模型 |

## ETF 量额与基金份额

`GET /api/v1/datasets/etf-trading` · [返回字段与供给说明](foundation-markets-macro.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `asset_code` | 是 | string | pattern="^[A-Z0-9][A-Z0-9.]{0,31}$" |
| query | `trade_date` | 是 | string | format="date" |

## 商品主力合约日线

`GET /api/v1/datasets/commodity-daily` · [返回字段与供给说明](foundation-markets-macro.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |
| query | `asset_code` | 是 | string | pattern="^[A-Z0-9][A-Z0-9.]{0,31}$" |
| query | `window_end` | 是 | string | format="date" |

## 股票涨跌停状态

`GET /api/v1/datasets/stock-limit-status` · [返回字段与供给说明](foundation-markets-macro.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `symbol` | 是 | string | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `trade_date` | 是 | string | format="date" |

## 标准宏观系列与修订

`GET /api/v1/datasets/macro-observations` · [返回字段与供给说明](foundation-markets-macro.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |
| query | `series_id` | 是 | string | minLength=1, maxLength=128 |
| query | `as_of` | 是 | string | format="date-time" |

## 专项宏观观测与来源时点

`GET /api/v1/datasets/macro-specialty-observations` · [返回字段与供给说明](foundation-markets-macro.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `start_date` | 是 | string | format="date" |
| query | `end_date` | 是 | string | format="date" |
| query | `limit` | 否 | integer | default=500, minimum=1, maximum=2000 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=256 |
| query | `series_id` | 是 | cn.gdp.nominal_cumulative / fx.usdcnh | 见请求模型 |
| query | `as_of` | 是 | string | format="date-time" |

## 可展示新闻、公告与文章

`GET /api/v1/datasets/content-documents` · [返回字段与供给说明](foundation-content.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `content_type` | 否 | news / announcement / article / null | 见请求模型 |
| query | `symbol` | 否 | string / null | pattern="^[0-9]{6}\\.(?:SH\|SZ\|BJ)$" |
| query | `published_from` | 否 | string / null | format="date-time" |
| query | `published_through` | 否 | string / null | format="date-time" |
| query | `limit` | 否 | integer | default=50, minimum=1, maximum=200 |
| query | `cursor` | 否 | string / null | minLength=1, maxLength=512 |

## 证券搜索

`GET /api/v1/search` · [返回字段与供给说明](security-search.md)

无需请求参数。

