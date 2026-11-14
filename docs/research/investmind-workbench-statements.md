# 工作台三大财报原件核验

2026-11-14 在开发库同一只读事务中，核对了全部 50 个已发布工作台的利润表、资产负债表和现金流量表。业务截止日均为 2026-10-21。48 个工作台的版本为 `20261021-1c01ff6eb0b7d78a`，另 2 个各有内容版本，逐证券保留在回执中。

## 字段与期间

| 报表 | 字段数 | 年报 | 一季报 | 中报 | 三季报 | 其他期末 | 来源有值 | 来源空值 | 派生有值 / 缺原料 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 利润表 | 29 | 921 | 815 | 844 | 783 | 2 | 78,068 | 16,152 | 2,773 / 592 |
| 资产负债表 | 42 | 910 | 791 | 832 | 760 | 0 | 104,613 | 33,693 | 0 / 0 |
| 现金流量表 | 62 | 900 | 811 | 835 | 776 | 0 | 106,646 | 95,996 | 3,261 / 61 |

报告期按“证券 × 报表类型 × 期末”计数。共 9,980 个报告期、441,855 个单元格。每股收益保留原字段精度，金额和空值逐格比对，派生行另行复算；全部与发布值一致。145,841 个来源空值出现在已选版本的原字段中。653 个派生值缺至少一个输入，同期冲突和所选版本缺字段的单元格均为 0。结果限于当前指针覆盖的这些科目及报告期。

## 从页面定位到原始记录

使用位置为 InvestMind 个股工作台 → 财务报表 → 利润表、资产负债表或现金流量表，并供风险下钻定位。响应位置为 `financial.statements.tables[kind]`。先用日期在 `periods` 定位列，再按 `rows[].label` 定位原字段。回执 `securities[证券].tables[表类型].reportSources[报告期]` 直接给出可见公告日及对应行引用；`tables[表类型][字段]` 给出有值、空值及缺原料的计数。[逐期来源及逐字段结果](investmind-workbench-statements.json)

| 表类型 | 供应商接口及库表 | 发布时间点与转换 |
| --- | --- | --- |
| `income` | Tushare `income` → `raw.source_payload`、原件行号 | 合并累计报表 `report_type=1`。毛利 `gross_profit=revenue−oper_cost` |
| `balance` | Tushare `balancesheet` → `raw.source_payload`、原件行号 | 期末存量。核对公司类型 `companyType`，用于财报展示时识别适用科目 |
| `cashflow` | Tushare `cashflow` → `raw.source_payload`，合并日常取得的 `core.cashflow_statement` 可见记录 | 合并累计流量。自由现金流 `free_cash_flow=n_cashflow_act−c_pay_acq_const_fiolta` |

举例，贵州茅台 2026-06-30 报表的可见公告日均为 2026-08-15。利润表对应 `raw.source_payload:3840:row:1`，资产负债表对应 `raw.source_payload:3841:row:2`，现金流量表同时对应 `raw.source_payload:3842:row:1` 与 `core.cashflow_statement:1`。现金流全池有 606 条被选中的 Core 行引用；与 Raw 同期且同版时，逐字段对账后保留同一发布值。

审计复用已验证的原件读取流程，检查冻结合同、成功批次、请求哈希、原件内容哈希、分页及原行数；之后独立选取截至业务日已披露的合并报表，在各证券、表类型和期末内按有效公告日、更新标记、原公告日依次取最新版本。按版本核对 `announcementDate`、`originalAnnouncementDate`、`updateFlag`、`sourceIds`、`capturedAt`、冲突字段及现金流请求字段。原件抓取日与用于历史可见性的公告日分别记录。所有运行步骤仅读当前本地来源。[重放脚本](../../scripts/audit-workbench-statements.py)

## 单位和页面口径

150 张表的金额单位标记均为 `yuan`。网站金额列将元除以 100,000,000 展示为亿元，`basic_eps`、`diluted_eps` 按元/股展示。`balance.total_share` 在当前界面按股数类处理，单元格保留原数值、未附股数单位，详情显示“股”，金额表头的亿元提示不能用于解释这一行。2026 中报的平安银行、贵州茅台、920002.BJ 原数值分别为 19,406,000,000、1,250,081,601、63,704,155。供应商原始股本字段的单位仍需独立字段合同佐证。

网站端另以这三只证券的 2026 中报为锚调用实际财报转换模块，逐表核对累计模式最近四年同期及单季模式连续四个季度，覆盖 9,576 个值、同比和环比结果，其中 5,737 个有值，3,839 个因基期、EPS 或原料规则返回空值。增长率用绝对基期作分母，本次遇到 278 个负基期和 24 个零基期比较。另核对了期初现金使用上季期末、期末现金及资产负债表直接取期末存量、除一季报外 EPS 不以累计差分。三股三表均与独立计算一致。本表报告期数和全量 API 核验与这次前端样本分开计数。底座以 `audit-website-projection-inputs.py statements` 提取数据，网站的 `scripts/audit-statement-projection.mjs` 读取标准输入，见[双仓运行方法](../audit-script-locations.md)。[网站财报转换层样本](investmind-statement-projection.json)

两个非标准期末利润表均能追到原件。920002.BJ 的 2022-08-31 对应 `raw.source_payload:3903:row:16`，920185.BJ 的 2014-05-31 对应 `raw.source_payload:3909:row:71`。前端 `canonicalReportPeriod` 只收标准季末日期，因此这两期在 API 保留，可在来源回执查询，不能通过年份和报告期选择器选到。

从配有后端依赖和数据库只读连接的仓库根目录运行 `python -I scripts/audit-workbench-statements.py --output docs/research/investmind-workbench-statements.json` 可按届时已发布范围重放。
