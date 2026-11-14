# 公共数据 API 请求参数

由生产 FastAPI 路由离线导出。各接口旁的供给文档记录返回字段、单位、原字段、数据库位置、更新节奏和历史边界。[机器可读请求规范](openapi-v1.json) · [字段消费者](field-consumers.md)

## 市场快讯

`GET /api/v1/news` · [返回字段与供给说明](news-api.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `cursor` | 否 | string / null | 见请求模型 |
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
| query | `version` | 否 | string / null | 见请求模型 |
| query | `view` | 否 | full / initial / scope | default="full" |

## 宏观观察、企业范围及采集状态

`GET /api/v1/macro/insights` · [返回字段与供给说明](macro-specialties.md)

| 位置 | 参数 | 是否必填 | 类型或可选值 | 默认值及约束 |
| --- | --- | --- | --- | --- |
| query | `version` | 否 | string / null | 见请求模型 |

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

## 证券搜索

`GET /api/v1/search` · [返回字段与供给说明](security-search.md)

无需请求参数。

