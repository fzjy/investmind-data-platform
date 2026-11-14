# 选股现金与财务质量指标的原件重算

业务日 2026-10-21、选股版本 `20261021-1c01ff6eb0b7d78a`。50 只证券的最新完整财年均为 2025 年。八项指标各取 FY、3 年、5 年、10 年四窗，共 1,600 个发布值。独立重算后，1,499 个数值在容差内相等，101 个因缺年份或科目两端均为空，差异 0 个。金额以元读入 Decimal，比值在最后一步作除法，输出数值与发布端浮点数比对，相对容差 10⁻⁹、绝对容差 10⁻⁸。[逐股原字段、出处、选择版本、发布值及重算值](research/investmind-screener-fiscal-cash.json)

## 从哪一份报告取数

只读加载与当前证券池匹配的最近一个完整财报补采合同。底座原件读取器校验已有合同、请求参数、返回内容哈希、分页、入库批次及行数，返回 Tushare `income/balancesheet/cashflow` 的原始响应行。另从 `core.income_statement/core.balance_sheet/core.cashflow_statement` 独立拉取年度行。工作台发布后的表格和选股器已有计算函数均未参与重算。输入候选中有 4,041 条原件年度行和 586 条核心年度行，这些数量包含早于计算窗的历史和同一期间的多个版本。

每个证券、财年、报表分别选择合并年度口径（12 月 31 日）。有效披露日优先用实际公告日，缺失则用原公告日，要求已披露且报告期、公告日不晚于业务日。优先累计报告 1 或 4，最后才用调整前报告 5；同级取最近有效披露日、同日优先调整报告 4、修订标记、原公告日。原件扩展提供报告 1，现金流核心表同版本可补充原件未请求的科目。同级来源对同一科目有冲突则保留空值。每次选择的原件页 ID、行号、核心行 ID、报告类型、修订标记和日期均在 `stockEvidence[证券].annualInputs[年度].reports`。

十年核验窗包含 485 个证券财年，实际选中 1,453 份证券×财年×报表。1,301 份只有原件依据，另 152 份为同版本原件加核心行，最终选中类型均为 1，相关科目同版冲突 0 个。47 只有十个可见年度，1 只有七年，2 只有四年。相同选择策略在其他业务日的报告类型、冲突和金额需随该业务日重放。

## 字段、公式与覆盖

每个多年度窗先逐年核全再累计。普通比率的分子分母分别求和，分母合计为零则为空。营业收入先读 `revenue`，缺值再读 `total_revenue`，本次 485 个证券财年均有前者。

| 字段 | 期间累计公式（下列原字段的金额单位均为元） | 可用证券数，FY／3 年／5 年／10 年 |
|---|---|---|
| `realMoneyIndex` | 经营活动现金净额 `n_cashflow_act` ÷ 归母净利 `n_income_attr_p` | 50／50／48／46 |
| `ocfToRevenue` | `n_cashflow_act` ÷ 营业收入 | 50／50／48／46 |
| `ocfToTotalLiab` | `n_cashflow_act` ÷ 各期末负债总额 `total_liab` 的累计值 | 50／50／48／46 |
| `capexCoverageRatio` | `n_cashflow_act` ÷ 购建长期资产付现 `c_pay_acq_const_fiolta` | 50／50／48／45 |
| `cashCollectionRatio` | 销售商品、提供劳务收现 `c_fr_sale_sg` ÷ 营业收入 | 44／44／42／39 |
| `cashPaymentRatio` | 购货付现 `c_paid_goods_s` ÷ 营业成本 `oper_cost` | 44／44／42／39 |
| `freeCashFlowFy` | （`n_cashflow_act` − `c_pay_acq_const_fiolta`）分年求和 ÷ 一亿，单位亿元 | 50／50／48／45 |
| `freeCashFlowMarginFy` | 同期自由现金流总额 ÷ 营收总额，比值 | 50／50／48／45 |

三种累计字段分别追加 `Cum3y/Cum5y/Cum10y`。自由现金流两个系列额外要求每年资本开支大于等于零。缺项列到证券、财年和原字段。例如 `000001.SZ` 的 2025 年现金流量表来自 `raw.source_payload:3764:row:3` 及同版 `core.cashflow_statement:19`，经营现金流 315,858,000,000 元、资本开支 2,893,000,000 元，重算及发布自由现金流均为 3,129.65 亿元。该报表缺 `c_fr_sale_sg`，收现比两端均空。

## 重放

在可读取数据库的底座根目录运行 `python -I scripts/audit-screener-fiscal-cash.py --output docs/research/investmind-screener-fiscal-cash.json`。依赖项目 Python 环境。脚本打开同一只读可重复读事务，将每股计算结果与 `mart.published_snapshot` 的选股研究字段逐项比较，如有差异会列出证券和数值并以非零状态退出。完整 API 到筛选成员的核对见 [选股器字段说明](screener-api-fields.md)。
