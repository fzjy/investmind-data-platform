# 股票工作台与同业研究接口

| 请求及参数 | 返回及消费位置 | 频率和历史 |
| --- | --- | --- |
| `GET /api/v1/stocks/{symbol}`，例如 600519.SH | data 含 quote、dailyChart、sidebar、metrics、risks、financial。InvestMind 股票工作台各区域从同一次响应取值；asOf、version、source 在 meta 中 | 报价和日线随交易日，财报随披露及修订，公告更新可生成独立内容版本。mart.published_snapshot 保存当前产品 |
| `POST /api/v1/stocks/research`，必填 symbol、可选 peerSymbol，两者必须不同 | current、peer、instruments、scope、metrics 与 pair。省略对手时从同币种同行按市值降序、代码升序取首只。已验证接口调用，页面直接消费关系仍在全站反查 | 每次按已发布池计算，无独立的研究结果历史。双腿行情须同源、同版、同日 |

| 字段 | 供应商与落库 | 计算与用途 |
| --- | --- | --- |
| quote.name、price、changePercent、previousClose | Tushare stock_basic → core.security.name；daily → core.daily_price.close、pct_chg、pre_close | 报价用于行情栏及估值输入。涨跌幅为百分数。失去前收时按收盘价及涨跌幅推算；遇缺涨跌或 -100% 用当前收盘 |
| quote.pe、peTtm、pb、marketCap | daily_basic → core.daily_basic.pe、data.pe_ttm、pb、total_mv | 静态 PE 和滚动 PE 分列，估值为倍数，市值从万元转亿元。研究 metrics.pe 使用静态 PE |
| dailyChart.candles 的 date、open、high、low、close、volume | daily → core.daily_price；adj_factor → core.adjustment_factor | 三个公历年，含首末日。四价乘当日可用因子再除以窗口最后因子，成交量用原值。缺可用因子时系数可为 1。用于日线和同日价格比 |
| metrics 中的市值、估值、净现金、五年分红次数及三种股息率 | 选股快照根字段 pe、pb、marketCap，其余来自 researchValues | 金额只在同币种比较，经验分位为小于或等于本值的比例，zScore 用样本标准差。比较缺一侧时差值留空 |
| risks.groups[].items、score、level | core.income_statement、balance_sheet、cashflow_statement、financial_indicator、dividend、daily_basic、daily_price 及已核验原件；derived.risk_result 保存当前每条规则输入、计算过程和结论 | workbench-risk-v3 每股 12 条，评分从 100 起，高风险扣 18、关注扣 10、未知扣 4，最低为零；未知超过 4 条时显示数据不足。风险区和财报下钻消费同一组卡片 |

旧入口 GET /api/data/stocks/{symbol} 和 POST /api/data/stocks/research 共用 v1 的处理逻辑。三大财报依赖、覆盖及逐期原件可查[三大报表核验](research/investmind-workbench-statements.md)。

**风险历史。** 当前 50 个指针有 600 张风险卡，31 股评分因覆盖不足为空。历史页快照 1,592 条的 payload 均无 risks；版本读取器对旧工作台返回“风险清单仅提供最新结果”。发布时仅保留当前风险明细。能否按当时的原料重算归清单 4.4。

**运行核验。** 50 股的 400 个报价字段、三年窗口内 36,037 根日线的日期和五个数值均与独立重算结果相符。三个研究请求覆盖手选、自动推荐及版本冲突。风险使用生产规则从实际原料重放，已核对全部 600 条完整卡片的接口值、当前落库行和快照版本，并独立复算 50 个汇总分。另从同一批可见输入独立计算 12 种规则的 600 个分级，全部吻合；其中分红支付事实共用已核实的事件归属逻辑。[行情逐股结果](research/investmind-workbench-quotes.json) · [研究请求](research/investmind-stock-research.json) · [风险覆盖与历史](research/investmind-workbench-risks.json)
