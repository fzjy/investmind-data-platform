# 市场总览与市场浏览的供给核验

2026-11-14 从本机 PostgreSQL 只读重放。两个 GET 接口分别返回一个完整市场产品；返回正文逐字段等于 `mart.published_snapshot.payload`，对应版本在 `mart.page_snapshot` 各有一份相同正文。两个指针均标记 `stale=true`、原因为 `SNAPSHOT_BEHIND_EXPECTED_DATE`。 [逐产品版本、字段日期、覆盖和源记录](research/investmind-market-supply.json)

| 接口与产品键 | 指针业务日 / 版本 | 已保存历史 |
|---|---|---|
| `GET /api/data/market-overview`，`market-overview` | 2026-10-21，`20261021-6319db232c0a22d0` | 5 个业务日、15 个版本。按 08-24、09-18、10-13、10-16、10-21 分别为 5、1、3、5、1 版。 |
| `GET /api/data/market-explorer`，`market-explorer` | 2026-10-21，`content-40840744fde0c83ab45b3d437e27f469` | 相同 5 个业务日、112 个版本，分别为 5、1、3、5、98 版。内容更新可生成同一行情日的新版本。 |

两个接口均读取当前发布指针，没有按历史日期或版本查询的参数。数据库保留的版本数、商品图表自带的日期序列，以及新闻自身发布时间分别计量。市场业务日期来自股票样本成交日。指数、ETF、商品、地域标签抓取和新闻各依其字段选定日期。

## 总览

| 字段与粒度 | 当前供给 | 来源、单位与核验深度 |
|---|---|---|
| `marketSnapshot,turnoverSnapshot,turnoverTop10,breadthSnapshots`，股票集合 × 日 | 10-21 固定 50 股。成交额 111,915,637.30952 千元；上涨 15、下跌 34、平 1，平均涨跌幅 −0.90%。 | 按同日选股池代码联结 `core.security/core.daily_price/core.daily_basic`。逐股按 Tushare `daily.amount,pct_chg,close` 汇总成交、宽度及十大成交额名次、金额与涨跌。覆盖为该证券池。 |
| `limitStats`，真实交易状态 | 涨停、跌停均空，有效覆盖 0/50。 | 同日 `daily_basic` 已入核心的 `data.limit_status` 非空 0 条。涨停状态码 2/3、跌停 5/6。无值独立于情绪指标的价格阈值。 |
| `sentimentSnapshot`，样本日 | `threshold-v1`，覆盖 50、收盘上涨 15、收盘达上阈值 0、到下阈值 1、日内触达上阈值 0。总分空，状态 `unobservable`。 | 按 `daily.close,pre_close,high,vol` 和上市日核对全部六个样本计数。阈值为 9.8%。上行触达数为零，回落比例无分母。 |
| `globalIndices`，指数 × 当地交易日 | 6 个指数；沪深创及恒指为 10-16，标普与纳指为 10-15。六条分时均无数据，交易日历状态均为 `unknown`。 | Tushare `index_daily/index_global` → `raw.source_payload` → `core.market_daily_quote` → 发布项。逐指数核实际交易日、抓取时刻、收盘价（展示两位小数）与涨跌幅。 |
| `etfQuotes`，基金 × 日 | 9 只均为 10-16；当前概览发布的是涨跌幅。 | Tushare `fund_daily` 原件与同日 `core.market_daily_quote`，逐只对账。浏览页的 ETF 规模另外依赖 `fund_share`。 |
| `marketStatuses,fundFlowSnapshot` | 交易状态数组为空；北向及主力流均为 `--`，状态“缺失”。 | 生产合约中有字段，本版没有可向下复算的资金流值。 |

## 浏览

| 字段与粒度 | 实际返回及所取日期 | 来源、单位与核验深度 |
|---|---|---|
| `heatmapCategories,heatmapData.majorIndustry/subIndustry` | 10-21 申万大类 18 组、细类 38 组，各覆盖 50 只。概念分类未发布。 | 从 `core.classification_member` 取有效期覆盖业务日的成员，沿 `core.classification` 同来源父级递推；按 `tushare / SW2021` 一级与三级逐组比对成员。50 只对应的价格、涨跌、市值（万元转亿元）、成交额（千元转亿元）以及分组聚合均已核对。 |
| `heatmapData.region,regionMetadata` | 10-21 按 20 个地区聚合 50 只；已知地域 50、未知 0。地域资料抓取为 10-17，详见证据的 UTC 时刻。 | `stock_basic.area` → `core.security.data.area`；逐只比对当前标签及同日行情。标签含城市，当前证券资料不能用作过去注册地沿革。这次追到核心行，未逐个重验地区资料原件。 |
| `heatmapData.etf` | 10-16 共 9 格；每格一只 ETF，`stocks=[]`、`stockCount=null`。 | 两份原件分别取 `fund_daily.close,pct_chg,vol,amount` 和 `fund_share.fd_share`，按同代码与交易日核对。万份 × 元 ÷ 10,000 为亿元规模；成交额千元 ÷ 100,000 为亿元。9 只均与原件一致，未取得基金成分股清单。 |
| `rankingLeaders,rankingDecliners` | 10-21 涨幅前十和后十，各 10 行。 | 以 50 股同日 `core.daily_price.pct_chg` 重排，逐行比对代码、收盘价和涨跌幅。 |
| `commodityCategories,commodityData,commodityPeriods,commodityMetadata` | 贵金属、有色、黑色、能源、农产品各 2 个国内主力品种，报价 10-16，日线窗为 06-19 至 10-16；海外 0、日内 0。 | 按品种对账 `fut_trade_cal,fut_mapping,fut_basic,fut_daily` 原件。交易日、主力合约映射及日线日期集合相同。790 根日 K 的 OHLC、量、额、持仓、昨收、结算等逐根核对；从日线重算 170 根周 K、50 根月 K 的七个聚合数值。未复权。该历史包实际涨跌字段依 `changeBasis` 取前结算口径，形成当前主力报价。 |
| `marketNews` | 实际正文 50 条新浪财经快讯，2026-11-14 11:03:15～14:29:46 UTC；全部为 `link_only`。 | 按 ID 对到 `core.content_document` 的标题、链接、来源、发布时间与授权状态。这里只核对已发布正文；历史页面的行情基准日不限制编辑更新后的新闻日期。 |

`missingData` 在两产品均列出资金流、海外商品、日内商品。指数与商品不能用产品外层的 10-21 当作自身最新交易日。区域/行业热力图、排名及宽度本次只复算最新 50 股样本；历史页核对了存在区间、版本分布及当前指针的同版正文，尚未逐历史版本重新聚合。指数/ETF/期货本次共核 88 页供应商原件的内容哈希，接口数量见证据中的 `verifiedRawPagesByApi`。ETF 概览和浏览用不同字段合同，底层可重复利用的数据仍由请求和采集去重阶段另行核算。

在底座根目录执行 `python -I scripts/audit-market-supply.py --output docs/research/investmind-market-supply.json`。脚本在同一个只读事务通过生产路由读取响应，从已存事实重算，全部断言成功后保存 JSON。该次核验无采集、发布或补数操作。
