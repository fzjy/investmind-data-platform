# 选股历史数据与浮层口径

业务日为 2026-10-21，快照版本为 `20261021-1c01ff6eb0b7d78a`，涵盖开发池 50 只证券。以下数量是界面可以读取的发布数据，包括有值条目和明确空值。原始数据 ID、报表类型、期间及分子分母可按证券检查。[逐股核验](research/investmind-screener-history.json)

| 位置 / 字段 | 原始数据和复算 | 结果 |
| --- | --- | --- |
| 增速详情里的快照同比参考，以及排除条件 | `revenueGrowth/profitGrowth/cashflowGrowth` 各自的 `derived.metric_value` 版本 1.0.0 → `derived.metric_input` 引用的 `core.income_statement` 或 `core.cashflow_statement` 本期及上年同期行。营业收入优先 total_revenue，再取 revenue；利润依次用 n_income_attr_p、n_income、total_profit；经营现金净额用 n_cashflow_act。按 (本期 − 基期) / abs(基期) ×100，四位小数。 | 三组各 50 个值与独立重算和派生表相等。本期、基期原列及实际选用的列见 JSON。同比参考的报表原料、别名顺序与图中的单季金额分别记录。 |
| 支付率主值与最新财年行 | 50 个根 `payoutRatio` 均为研究层三年均值镜像，40 个有值、10 个双空。最近单期取 `listHistory.dividends[]` 最大年份的同年已派总额 / 同年归母净利 ×100；记录缺失时保留空值。原始分红事件及利润选版见[分红重算](screener-capital-fields.md)。 | 最新年度均为 2025。50 个单期值吻合，48 只的三年均值与最新年度值不同或缺失。浮层已改为展示带年份的年度值；有新期但支付率缺失时保留该年和空值。 |
| 收益率明细与折线 | `listHistory.returns[]` 按业务日选择可见的 `core.financial_indicator` 报告修订。只接收自然季末。逐字段核对报告类型、有效公告日 / 更新标记、同版冲突及供应商 roe、roic 原百分数。 | 604 个报告期，ROE 604 个数值相等，ROIC 532 个相等、72 个双空。年报或季报视图取相应报告最近八期，ROIC 筛选窗另见年报证据。 |
| 机构持仓历史 | `top10_holders` 对应 `core.top_holder`。按报告期、当时可见最新公告、持有人名称去重。优先用 hold_ratio，缺时才从持股数和规则允许时窗内的总股本还原。按项目持有人映射规则汇总。 | 400 期中 362 个数值相等，38 个双空，其中 37 期无法判断机构种类，1 期持股比例之和异常。逐期引用核心行与股本行。 |
| 股东户数历史与筹码卡片 | 已接受的 Tushare `stk_holdernumber` 原件与 `core.shareholder_count`，按截至日可见公告、期末日及同日同值筛选。对所有最新可见观察核对原始内容哈希，保留原件 ID、零起算行号和 Core 行号。 | 400 个期间中 397 个值匹配，3 个双空均为平安银行，同期同日存在原件空值与有值冲突。例：2025-10-31 同时观察到 453,515 户与空值。卡片可从已验证的最近两次非缺失条件计算变化；研究层筛选键仍未发布其自身口径。 |
| 大类、细分、地域、板块及概念 | 从 `core.classification_member` 选业务日生效成员，从 `core.classification` 逐级追溯 Tushare SW2021。核对成员证据引用的 `index_member_all` 原件行，一级、三级名称、证券及生效日吻合。地域核对带哈希的 `stock_basic.area` 原件，板块依 `core.security.board/exchange` 及既有映射。 | 50 只各自的行业、大类、细分、地域、板块完全对应；50 只行业与地域均有原件引用。概念标签均与证券扩展数据的空列表一致。此版本未取得可用于验证正标签的概念成员数据。行业是在已存成员记录上按业务生效日期回溯，无法据此确认历史投资者当时已知的分类。 |

运行 `python -I scripts/audit-screener-history.py --output docs/research/investmind-screener-history.json` 可在只读数据库重放。报告和受益股东类型缺失按字段保留，保留分母为零和同版冲突的区别。程序使用既有资本指标审计的独立机构比例算式，按全部最近八期复算，其他字段未调用选股结果计算函数。
