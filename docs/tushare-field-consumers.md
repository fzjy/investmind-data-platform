# Tushare 原字段到 InvestMind 消费者

每行标识一个接口的一个响应字段。对于同名字段，须连同接口搜索。直接和间接用途可并存，汇总优先显示直接。未知项保留采集状态，已确认退役的旧合同保留历史。全量机器可读回执包含来源契约、Raw 示例 ID、Core 实测非空行数及各个页面或任务的证据。

[完整逐字段回执](research/investmind-tushare-field-consumers.json) · [只读重放脚本](../scripts/audit-tushare-field-consumers.py)

本次 50 个端点共 565 项，其中 直接 178、间接 231、仅历史 8、未知 148。564 项存在于登记的现行或离线契约，400 项有真实返回列。50 个发布工作台中出现 131 项同名来源报表行。

## 如何读表

- Raw 数是包含该返回列的留存原件数，空数组保留表头也计数。JSON 中可分开查看仅表头原件数。宏观窗口按原件去重。HTTP 错误体和只有请求配置的空响应不提供实际字段见证。
- Core 对应数据集表为标准归一化路径。同一字段可在普通列或 data JSON 中存在。回执按全表统计实际键，无法据同名键推定某条记录来自某个调用。三表专题直接从 Raw 取版，现金流还能合并 Core。专题数据可能绕过常规 Core 落库，实际数据集及原件 ID 可区分路径。
- VIP 批量端点独立统计。未采到对应端点的原件时，查同名标准端点可了解下游设计，当前 VIP 来源到生产页面的关系仍待实证。
- 已发布动态行区分存在和有值。原件到财报单元格的逐期校验见工作台三表专题。静态公式和别名表证明代码会读取，未逐项目证明每个备选在某日实际命中。可能还存在未命名的动态消费者或其他项目。

## 默认采集与历史兼容

- 业绩快报 `express` 的七个字段没有出现在发布或来源签名代码。新日增量、全池回填（标准和 VIP）均停止规划快报。已有原件、Core、显式按数据集回填和旧失败项的显式重放保留。底座不删除已有行。
- 通用主营合同 `fina_mainbz.revenue/segment_type/segment_name` 与专题的 `bz_sales/bz_item` 分开核对。三个通用字段均没有实际返回列或同名 Core 非空值。旧格式的 `segment_name` 参与写入身份，旧格式整行可进入财务来源签名。当前不具备安全移除通用合同的证据。主营专题继续按财报期和产品/地域采集。
- `fut_basic.name`、`index_basic.base_date/list_date/desc`、`index_member_all.name` 参与已有原件的完整字段合同、请求键或游标哈希复核。没有找到直接展示这五个值的页面；每行列明用到该合同的任务。收窄合同需区分旧版重放与新版请求，再验证商品、全收益基准和申万成员三个持久化读取器。
- 7 个 VIP 端点映射到同名标准端点，合计 146 个字段与标准端点逐一同名。当前本地模式和原件记录在回执；无 VIP 响应见证。相同字段的标准端点消费关系可按对应端点反查。显式选择 VIP 且账号具备权限才可按报告期取数；快报默认停采，显式回填可单独选择。未向供应商探测账号实际权限。
- 现金流、应收与订单等企业补充独立写入 `raw.enterprise_*_response`，指数候选写入 `raw.enterprise_target_observation`。这些表的字段回执与通用原件分列；目标候选并不自动进入网站展示。

## 来源字段

| 接口.字段 | 归一化数据集或专题原件 | Raw 数 | 状态 | 已登记用途 |
| --- | --- | ---: | --- | --- |
| `adj_factor.adj_factor` | core.adjustment_factor | 214 | 间接 | 工作台/日 K 复权序列；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `adj_factor.trade_date` | core.adjustment_factor | 214 | 间接 | 工作台/日 K 复权序列；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `adj_factor.ts_code` | core.adjustment_factor | 214 | 间接 | 工作台/日 K 复权序列；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.accounts_receiv` | core.balance_sheet / raw.enterprise_receivable_response / 财报专题 Raw | 632 | 直接 | 工作台/财务报表/financial.statements.tables.rows[accounts_receiv]；宏观/企业应收与订单补充见证；工作台/比率分析/metrics.fields[accountsReceiv]（按别名顺序取首个有效值） |
| `balancesheet.acct_payable` | core.balance_sheet / raw.enterprise_rea_response / 财报专题 Raw | 632 | 直接 | 工作台/财务报表/financial.statements.tables.rows[acct_payable]；宏观/企业 REA 五节点补充见证与取版；工作台/比率分析/metrics.fields[acctPayable]（按别名顺序取首个有效值） |
| `balancesheet.adv_receipts` | core.balance_sheet / 财报专题 Raw | 81 | 直接 | 工作台/财务报表/financial.statements.tables.rows[adv_receipts]；工作台/比率分析/metrics.fields[advReceipts]（按别名顺序取首个有效值） |
| `balancesheet.ann_date` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.bond_payable` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[bond_payable]；宏观/企业 REA 五节点补充见证与取版；工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.cap_rese` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[cap_rese] |
| `balancesheet.cip` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[cip] |
| `balancesheet.comp_type` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/适用行业 companyType；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.contract_assets` | core.balance_sheet / 财报专题 Raw | 81 | 直接 | 工作台/财务报表/financial.statements.tables.rows[contract_assets]；工作台/比率分析/metrics.fields[contractAssets]（按别名顺序取首个有效值） |
| `balancesheet.contract_liab` | core.balance_sheet / raw.enterprise_order_response / 财报专题 Raw | 632 | 直接 | 工作台/财务报表/financial.statements.tables.rows[contract_liab]；宏观/企业应收与订单补充见证；工作台/比率分析/metrics.fields[contractLiab]（按别名顺序取首个有效值） |
| `balancesheet.defer_tax_assets` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[defer_tax_assets] |
| `balancesheet.defer_tax_liab` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[defer_tax_liab] |
| `balancesheet.end_date` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.f_ann_date` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.fix_assets` | core.balance_sheet / raw.enterprise_rea_response / 财报专题 Raw | 632 | 直接 | 工作台/财务报表/financial.statements.tables.rows[fix_assets]；宏观/企业 REA 五节点补充见证与取版 |
| `balancesheet.goodwill` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[goodwill]；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.intan_assets` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[intan_assets] |
| `balancesheet.inventories` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[inventories]；工作台/比率分析/metrics.fields[inventory]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.invest_real_estate` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[invest_real_estate] |
| `balancesheet.lease_liab` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response | 1476 | 间接 | 工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.lt_amor_exp` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[lt_amor_exp] |
| `balancesheet.lt_borr` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[lt_borr]；宏观/企业 REA 五节点补充见证与取版；工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.lt_eqt_invest` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[lt_eqt_invest] |
| `balancesheet.minority_int` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[minority_int] |
| `balancesheet.money_cap` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[money_cap]；宏观/企业 REA 五节点补充见证与取版；工作台/估值与财务指标卡的报告取版；工作台/比率分析/metrics.fields[cash]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.non_cur_liab_due_1y` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[non_cur_liab_due_1y]；宏观/企业 REA 五节点补充见证与取版；工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.notes_payable` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[notes_payable]；工作台/比率分析/metrics.fields[notesPayable]（按别名顺序取首个有效值） |
| `balancesheet.notes_receiv` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[notes_receiv]；工作台/比率分析/metrics.fields[notesReceiv]（按别名顺序取首个有效值） |
| `balancesheet.oth_cur_assets` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_cur_assets] |
| `balancesheet.oth_cur_liab` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_cur_liab] |
| `balancesheet.oth_nca` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_nca] |
| `balancesheet.payroll_payable` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[payroll_payable] |
| `balancesheet.prepayment` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[prepayment]；工作台/比率分析/metrics.fields[prepayment]（按别名顺序取首个有效值） |
| `balancesheet.report_type` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.st_bonds_payable` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response | 1476 | 间接 | 工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.st_borr` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[st_borr]；宏观/企业 REA 五节点补充见证与取版；工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.surplus_rese` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[surplus_rese] |
| `balancesheet.taxes_payable` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[taxes_payable] |
| `balancesheet.total_assets` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_assets]；工作台/比率分析/metrics.fields[totalAssets]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.total_cur_assets` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_cur_assets]；工作台/比率分析/metrics.fields[currentAssets]（按别名顺序取首个有效值） |
| `balancesheet.total_cur_liab` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_cur_liab]；工作台/比率分析/metrics.fields[currentLiab]（按别名顺序取首个有效值） |
| `balancesheet.total_hldr_eqy_exc_min_int` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_hldr_eqy_exc_min_int]；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.total_hldr_eqy_inc_min_int` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_hldr_eqy_inc_min_int]；工作台/比率分析/metrics.fields[totalEquity]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算 |
| `balancesheet.total_liab` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_liab]；工作台/比率分析/metrics.fields[totalLiabilities]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/ocfToTotalLiab（FY、3/5/10 年） |
| `balancesheet.total_nca` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_nca] |
| `balancesheet.total_ncl` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_ncl] |
| `balancesheet.total_share` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1638 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_share]；工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.trad_asset` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[trad_asset] |
| `balancesheet.ts_code` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet.undistr_porfit` | core.balance_sheet / 财报专题 Raw | 162 | 直接 | 工作台/财务报表/financial.statements.tables.rows[undistr_porfit] |
| `balancesheet.update_flag` | core.balance_sheet / raw.enterprise_order_response, raw.enterprise_rea_response, raw.enterprise_receivable_response / 财报专题 Raw | 1642 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `balancesheet_vip.ann_date` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.bond_payable` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.comp_type` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.end_date` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.f_ann_date` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.goodwill` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.inventories` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.lease_liab` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.lt_borr` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.money_cap` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.non_cur_liab_due_1y` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.report_type` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.st_bonds_payable` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.st_borr` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.total_assets` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.total_hldr_eqy_exc_min_int` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.total_liab` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.total_share` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.ts_code` | core.balance_sheet | 0 | 未知 | 待核 |
| `balancesheet_vip.update_flag` | core.balance_sheet | 0 | 未知 | 待核 |
| `bse_mapping.n_code` | raw.source_payload | 1 | 间接 | 组合/分红证据中新旧证券代码归并 |
| `bse_mapping.o_code` | raw.source_payload | 1 | 间接 | 组合/分红证据中新旧证券代码归并 |
| `cashflow.amort_intang_assets` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 629 | 直接 | 工作台/财务报表/financial.statements.tables.rows[amort_intang_assets]；宏观/企业 REA 五节点补充见证与取版 |
| `cashflow.ann_date` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.c_cash_equ_beg_period` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_cash_equ_beg_period] |
| `cashflow.c_cash_equ_end_period` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 754 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_cash_equ_end_period]；工作台/比率分析/metrics.fields[cashEnding]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.c_disp_withdrwl_invest` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_disp_withdrwl_invest] |
| `cashflow.c_fr_oth_operate_a` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_fr_oth_operate_a] |
| `cashflow.c_fr_sale_sg` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_fr_sale_sg]；工作台/比率分析/metrics.fields[salesCash]（按别名顺序取首个有效值）；选股/cashCollectionRatio（FY、3/5/10 年） |
| `cashflow.c_inf_fr_operate_a` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_inf_fr_operate_a] |
| `cashflow.c_paid_for_taxes` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_paid_for_taxes] |
| `cashflow.c_paid_goods_s` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_paid_goods_s]；工作台/比率分析/metrics.fields[purchaseCash]（按别名顺序取首个有效值）；选股/cashPaymentRatio（FY、3/5/10 年） |
| `cashflow.c_paid_invest` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_paid_invest] |
| `cashflow.c_paid_to_for_empl` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 688 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_paid_to_for_empl]；宏观/企业 REA 五节点补充见证与取版 |
| `cashflow.c_pay_acq_const_fiolta` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_pay_acq_const_fiolta]；宏观/企业 REA 五节点补充见证与取版；工作台/比率分析/metrics.fields[capex]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/capexCoverageRatio, freeCashFlowFy, freeCashFlowMarginFy（FY、3/5/10 年） |
| `cashflow.c_pay_claims_orig_inco` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_pay_claims_orig_inco] |
| `cashflow.c_pay_dist_dpcp_int_exp` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 754 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_pay_dist_dpcp_int_exp]；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.c_prepay_amt_borr` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_prepay_amt_borr] |
| `cashflow.c_recp_borrow` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_recp_borrow]；工作台/风险面板的报告筛选及计算 |
| `cashflow.c_recp_cap_contrib` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 754 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_recp_cap_contrib]；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/capitalReturnRatio 及累计窗口 |
| `cashflow.c_recp_return_invest` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[c_recp_return_invest] |
| `cashflow.comp_type` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.depr_fa_coga_dpba` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 629 | 直接 | 工作台/财务报表/financial.statements.tables.rows[depr_fa_coga_dpba]；宏观/企业 REA 五节点补充见证与取版 |
| `cashflow.eff_fx_flu_cash` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[eff_fx_flu_cash] |
| `cashflow.end_date` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.f_ann_date` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.free_cashflow` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 695 | 直接 | 工作台/财务报表/financial.statements.tables.rows[free_cashflow]；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.ifc_cash_incr` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[ifc_cash_incr] |
| `cashflow.incl_cash_rec_saims` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[incl_cash_rec_saims] |
| `cashflow.incl_dvd_profit_paid_sc_ms` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[incl_dvd_profit_paid_sc_ms] |
| `cashflow.lt_amort_deferred_exp` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 629 | 直接 | 工作台/财务报表/financial.statements.tables.rows[lt_amort_deferred_exp]；宏观/企业 REA 五节点补充见证与取版 |
| `cashflow.n_cap_incr_repur` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_cap_incr_repur] |
| `cashflow.n_cash_flows_fnc_act` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_cash_flows_fnc_act]；工作台/比率分析/metrics.fields[financingCashFlow]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.n_cashflow_act` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_cashflow_act]；工作台/比率分析/metrics.fields[operatingCashFlow]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/realMoneyIndex, ocfToRevenue, ocfToTotalLiab, capexCoverageRatio, freeCashFlowFy, freeCashFlowMarginFy（FY、3/5/10 年） |
| `cashflow.n_cashflow_inv_act` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_cashflow_inv_act]；工作台/比率分析/metrics.fields[investingCashFlow]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.n_depos_incr_fi` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_depos_incr_fi] |
| `cashflow.n_disp_subs_oth_biz` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_disp_subs_oth_biz] |
| `cashflow.n_inc_borr_oth_fi` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_inc_borr_oth_fi] |
| `cashflow.n_incr_cash_cash_equ` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_cash_cash_equ]；工作台/比率分析/metrics.fields[netCashChange]（按别名顺序取首个有效值） |
| `cashflow.n_incr_clt_loan_adv` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_clt_loan_adv] |
| `cashflow.n_incr_dep_cbob` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_dep_cbob] |
| `cashflow.n_incr_disp_faas` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_disp_faas] |
| `cashflow.n_incr_disp_tfa` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_disp_tfa] |
| `cashflow.n_incr_insured_dep` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_insured_dep] |
| `cashflow.n_incr_loans_cb` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_loans_cb] |
| `cashflow.n_incr_loans_oth_bank` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_loans_oth_bank] |
| `cashflow.n_incr_pledge_loan` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_incr_pledge_loan] |
| `cashflow.n_recp_disp_fiolta` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_recp_disp_fiolta] |
| `cashflow.n_recp_disp_sobu` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_recp_disp_sobu] |
| `cashflow.n_reinsur_prem` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_reinsur_prem] |
| `cashflow.net_cash_rece_sec` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[net_cash_rece_sec] |
| `cashflow.net_dism_capital_add` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[net_dism_capital_add] |
| `cashflow.oth_cash_pay_oper_act` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_cash_pay_oper_act] |
| `cashflow.oth_cash_recp_ral_fnc_act` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_cash_recp_ral_fnc_act] |
| `cashflow.oth_cashpay_ral_fnc_act` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_cashpay_ral_fnc_act] |
| `cashflow.oth_pay_ral_inv_act` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_pay_ral_inv_act] |
| `cashflow.oth_recp_ral_inv_act` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oth_recp_ral_inv_act] |
| `cashflow.pay_comm_insur_plcy` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[pay_comm_insur_plcy] |
| `cashflow.pay_handling_chrg` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[pay_handling_chrg] |
| `cashflow.prem_fr_orig_contr` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[prem_fr_orig_contr] |
| `cashflow.proc_issue_bonds` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[proc_issue_bonds]；工作台/风险面板的报告筛选及计算 |
| `cashflow.recp_tax_rends` | core.cashflow_statement / 财报专题 Raw | 59 | 直接 | 工作台/财务报表/financial.statements.tables.rows[recp_tax_rends] |
| `cashflow.report_type` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.st_cash_out_act` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[st_cash_out_act] |
| `cashflow.stot_cash_in_fnc_act` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[stot_cash_in_fnc_act] |
| `cashflow.stot_cashout_fnc_act` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[stot_cashout_fnc_act] |
| `cashflow.stot_inflows_inv_act` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[stot_inflows_inv_act] |
| `cashflow.stot_out_inv_act` | core.cashflow_statement / 财报专题 Raw | 177 | 直接 | 工作台/财务报表/financial.statements.tables.rows[stot_out_inv_act] |
| `cashflow.ts_code` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow.update_flag` | core.cashflow_statement / raw.enterprise_rea_response / 财报专题 Raw | 758 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `cashflow_vip.amort_intang_assets` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.ann_date` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_cash_equ_beg_period` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_cash_equ_end_period` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_disp_withdrwl_invest` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_fr_oth_operate_a` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_fr_sale_sg` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_inf_fr_operate_a` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_paid_for_taxes` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_paid_goods_s` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_paid_invest` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_paid_to_for_empl` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_pay_acq_const_fiolta` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_pay_claims_orig_inco` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_pay_dist_dpcp_int_exp` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_prepay_amt_borr` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_recp_borrow` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_recp_cap_contrib` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.c_recp_return_invest` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.comp_type` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.depr_fa_coga_dpba` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.eff_fx_flu_cash` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.end_date` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.f_ann_date` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.free_cashflow` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.ifc_cash_incr` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.incl_cash_rec_saims` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.incl_dvd_profit_paid_sc_ms` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.lt_amort_deferred_exp` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_cap_incr_repur` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_cash_flows_fnc_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_cashflow_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_cashflow_inv_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_depos_incr_fi` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_disp_subs_oth_biz` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_inc_borr_oth_fi` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_cash_cash_equ` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_clt_loan_adv` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_dep_cbob` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_disp_faas` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_disp_tfa` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_insured_dep` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_loans_cb` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_loans_oth_bank` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_incr_pledge_loan` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_recp_disp_fiolta` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_recp_disp_sobu` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.n_reinsur_prem` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.net_cash_rece_sec` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.net_dism_capital_add` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.oth_cash_pay_oper_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.oth_cash_recp_ral_fnc_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.oth_cashpay_ral_fnc_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.oth_pay_ral_inv_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.oth_recp_ral_inv_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.pay_comm_insur_plcy` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.pay_handling_chrg` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.prem_fr_orig_contr` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.proc_issue_bonds` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.recp_tax_rends` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.report_type` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.st_cash_out_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.stot_cash_in_fnc_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.stot_cashout_fnc_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.stot_inflows_inv_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.stot_out_inv_act` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.ts_code` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cashflow_vip.update_flag` | core.cashflow_statement | 0 | 未知 | 待核 |
| `cn_cpi.month` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.observed_at（系列 cn.cpi.yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.cpi.yoy（供应商配置，实际来源按版本） |
| `cn_cpi.nt_yoy` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.cpi.yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.cpi.yoy（供应商配置，实际来源按版本） |
| `cn_gdp.gdp` | raw.source_payload / 宏观观测按发布合同 | 1 | 间接 | 宏观/名义 GDP、实际同比诊断、季度 |
| `cn_gdp.gdp_yoy` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.gdp.real_yoy 的 Tushare 报告期或数值，经转换与去重/修订，40 行） | 12 | 间接 | 宏观/名义 GDP、实际同比诊断、季度；宏观/指标序列 cn.gdp.real_yoy（供应商配置，实际来源按版本） |
| `cn_gdp.quarter` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.observed_at（系列 cn.gdp.real_yoy 的 Tushare 报告期或数值，经转换与去重/修订，40 行） | 12 | 间接 | 宏观/名义 GDP、实际同比诊断、季度；宏观/指标序列 cn.gdp.real_yoy（供应商配置，实际来源按版本） |
| `cn_m.m1_yoy` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.money.m1_yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.money.m1_yoy（供应商配置，实际来源按版本） |
| `cn_m.m2_yoy` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.money.m2_yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.money.m2_yoy（供应商配置，实际来源按版本） |
| `cn_m.month` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.observed_at（系列 cn.money.m1_yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） / core.macro_observation.observed_at（系列 cn.money.m2_yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 22 | 间接 | 宏观/指标序列 cn.money.m1_yoy（供应商配置，实际来源按版本）；宏观/指标序列 cn.money.m2_yoy（供应商配置，实际来源按版本） |
| `cn_pmi.month` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.observed_at（系列 cn.pmi.manufacturing 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.pmi.manufacturing（供应商配置，实际来源按版本） |
| `cn_pmi.pmi010000` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.pmi.manufacturing 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.pmi.manufacturing（供应商配置，实际来源按版本） |
| `cn_ppi.month` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.observed_at（系列 cn.ppi.yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.ppi.yoy（供应商配置，实际来源按版本） |
| `cn_ppi.ppi_yoy` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.ppi.yoy 的 Tushare 报告期或数值，经转换与去重/修订，120 行） | 11 | 间接 | 宏观/指标序列 cn.ppi.yoy（供应商配置，实际来源按版本） |
| `daily.amount` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.change` | core.daily_price | 223 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.close` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/预期、NTM 与财年股息率；组合/持仓行情 |
| `daily.high` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.low` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.open` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.pct_chg` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.pre_close` | core.daily_price | 223 | 直接 | 工作台/昨收；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.trade_date` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.ts_code` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily.vol` | core.daily_price | 223 | 直接 | 工作台/行情与日 K；市场/成交、涨跌；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.circ_mv` | core.daily_basic | 218 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.dv_ratio` | core.daily_basic | 218 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；股票发布/股息率输入与备选 |
| `daily_basic.dv_ttm` | core.daily_basic | 218 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；股票发布/股息率输入与备选 |
| `daily_basic.float_share` | core.daily_basic | 212 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/持仓占比；工作台/证券股本输入 |
| `daily_basic.free_share` | core.daily_basic | 212 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.limit_status` | core.daily_basic | 112 | 间接 | 市场/涨跌停家数；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.pb` | core.daily_basic | 318 | 直接 | 选股、工作台/静态 PE、PB、市值、滚动 PE 各自对应同名口径；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/pePercentile、pbPercentile；银行长期估值 |
| `daily_basic.pe` | core.daily_basic | 318 | 直接 | 选股、工作台/静态 PE、PB、市值、滚动 PE 各自对应同名口径；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/pePercentile、pbPercentile；银行长期估值 |
| `daily_basic.pe_ttm` | core.daily_basic | 212 | 直接 | 选股、工作台/静态 PE、PB、市值、滚动 PE 各自对应同名口径；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.ps` | core.daily_basic | 218 | 间接 | 工作台/PS 估值带数据优先级；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.ps_ttm` | core.daily_basic | 212 | 间接 | 工作台/PS 估值带数据优先级；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.total_mv` | core.daily_basic | 218 | 直接 | 选股、工作台/静态 PE、PB、市值、滚动 PE 各自对应同名口径；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.total_share` | core.daily_basic | 212 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/持仓占比；工作台/证券股本输入 |
| `daily_basic.trade_date` | core.daily_basic | 368 | 间接 | 工作台/证券交易日关联行情与估值；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.ts_code` | core.daily_basic | 368 | 间接 | 工作台/证券交易日关联行情与估值；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_basic.turnover_rate` | core.daily_basic | 218 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `daily_info.exchange` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/A 股流通市值交易所详细源 |
| `daily_info.float_mv` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/A 股流通市值交易所详细源 |
| `daily_info.trade_date` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/A 股流通市值交易所详细源 |
| `daily_info.ts_code` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/A 股流通市值交易所详细源 |
| `disclosure_date.actual_date` | core.disclosure_schedule | 151 | 间接 | 选股、工作台/已披露期与补采 |
| `disclosure_date.ann_date` | core.disclosure_schedule | 151 | 间接 | 股票任务/披露日历修订比较 |
| `disclosure_date.end_date` | core.disclosure_schedule | 151 | 间接 | 选股、工作台/已披露期与补采 |
| `disclosure_date.modify_date` | core.disclosure_schedule | 151 | 间接 | 股票任务/披露日历修订比较 |
| `disclosure_date.pre_date` | core.disclosure_schedule | 151 | 间接 | 股票任务/披露日历修订比较 |
| `disclosure_date.ts_code` | core.disclosure_schedule | 151 | 间接 | 选股、工作台/已披露期与补采 |
| `dividend.ann_date` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.base_date` | core.dividend | 102 | 间接 | 组合/现金及股本事件去重、基数与实施公告 |
| `dividend.base_share` | core.dividend | 102 | 间接 | 组合/现金及股本事件去重、基数与实施公告 |
| `dividend.cash_div` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.cash_div_tax` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.div_listdate` | core.dividend | 102 | 间接 | 组合/证券匹配与送转到账日 |
| `dividend.div_proc` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.end_date` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.ex_date` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.imp_ann_date` | core.dividend | 303 | 间接 | 组合/现金及股本事件去重、基数与实施公告；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `dividend.pay_date` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.record_date` | core.dividend | 365 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、工作台、组合/实施版本、财年归属、股息到账及收益率 |
| `dividend.stk_bo_rate` | core.dividend | 102 | 间接 | 组合/现金及股本事件去重、基数与实施公告 |
| `dividend.stk_co_rate` | core.dividend | 102 | 间接 | 组合/现金及股本事件去重、基数与实施公告 |
| `dividend.stk_div` | core.dividend | 102 | 间接 | 组合/现金及股本事件去重、基数与实施公告 |
| `dividend.ts_code` | core.dividend | 365 | 间接 | 组合/证券匹配与送转到账日；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `etf_basic.csname` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `etf_basic.etf_type` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `etf_basic.exchange` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `etf_basic.index_code` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `etf_basic.index_name` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `etf_basic.list_status` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `etf_basic.ts_code` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `express.ann_date` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express.end_date` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express.n_income` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express.revenue` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express.total_assets` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express.total_hldr_eqy_exc_min_int` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express.ts_code` | core.financial_express | 62 | 仅历史 | 业绩快报/日增量和全池回填默认停采。已有 Raw、Core 和按需重放保留 |
| `express_vip.ann_date` | core.financial_express | 0 | 未知 | 待核 |
| `express_vip.end_date` | core.financial_express | 0 | 未知 | 待核 |
| `express_vip.n_income` | core.financial_express | 0 | 未知 | 待核 |
| `express_vip.revenue` | core.financial_express | 0 | 未知 | 待核 |
| `express_vip.total_assets` | core.financial_express | 0 | 未知 | 待核 |
| `express_vip.total_hldr_eqy_exc_min_int` | core.financial_express | 0 | 未知 | 待核 |
| `express_vip.ts_code` | core.financial_express | 0 | 未知 | 待核 |
| `fina_indicator.ann_date` | core.financial_indicator | 72 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.bps` | core.financial_indicator | 0 | 间接 | 工作台/估值与财务指标卡的报告取版 |
| `fina_indicator.debt_to_eqt` | core.financial_indicator | 66 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.end_date` | core.financial_indicator | 72 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.eps` | core.financial_indicator | 0 | 间接 | 工作台/估值与财务指标卡的报告取版 |
| `fina_indicator.fcff` | core.financial_indicator | 66 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.grossprofit_margin` | core.financial_indicator | 70 | 直接 | 选股/毛利率、净利率；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.interestdebt` | core.financial_indicator | 66 | 间接 | 工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.invest_capital` | core.financial_indicator | 66 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；股票派生/税后营业利润、投入资本原料 |
| `fina_indicator.netdebt` | core.financial_indicator | 66 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.netprofit_margin` | core.financial_indicator | 70 | 直接 | 选股/毛利率、净利率；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.netprofit_yoy` | core.financial_indicator | 70 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.ocf_to_or` | core.financial_indicator | 66 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.or_yoy` | core.financial_indicator | 70 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.q_eps` | core.financial_indicator | 2 | 间接 | 工作台/估值与财务指标卡的报告取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.q_roe` | core.financial_indicator | 2 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.roe` | core.financial_indicator | 70 | 直接 | 选股/ROE 最近报告、ROIC 最近有效 FY；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.roic` | core.financial_indicator | 70 | 直接 | 选股/ROE 最近报告、ROIC 最近有效 FY；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.tax_to_ebt` | core.financial_indicator | 66 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；股票派生/税后营业利润、投入资本原料 |
| `fina_indicator.ts_code` | core.financial_indicator | 72 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator.update_flag` | core.financial_indicator | 68 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_indicator_vip.ann_date` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.bps` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.debt_to_eqt` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.end_date` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.eps` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.fcff` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.grossprofit_margin` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.interestdebt` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.invest_capital` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.netdebt` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.netprofit_margin` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.netprofit_yoy` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.ocf_to_or` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.or_yoy` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.q_eps` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.q_roe` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.roe` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.roic` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.tax_to_ebt` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.ts_code` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_indicator_vip.update_flag` | core.financial_indicator | 0 | 未知 | 待核 |
| `fina_mainbz.bz_cost` | core.business_segment | 383 | 间接 | 工作台/主营构成，按产品/地区分类、币种核对 |
| `fina_mainbz.bz_item` | core.business_segment | 383 | 间接 | 工作台/主营构成，按产品/地区分类、币种核对 |
| `fina_mainbz.bz_profit` | core.business_segment | 383 | 间接 | 工作台/主营构成，按产品/地区分类、币种核对 |
| `fina_mainbz.bz_sales` | core.business_segment | 383 | 间接 | 工作台/主营构成，按产品/地区分类、币种核对 |
| `fina_mainbz.curr_type` | core.business_segment | 383 | 间接 | 工作台/主营构成，按产品/地区分类、币种核对 |
| `fina_mainbz.end_date` | core.business_segment | 445 | 间接 | 工作台/主营业务来源证券、报告期及修订校验；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_mainbz.revenue` | core.business_segment | 0 | 未知 | 待核 |
| `fina_mainbz.segment_name` | core.business_segment | 0 | 间接 | 通用主营归一化/旧格式 Core 记录的更新身份；当前来自供应商的同名值未取证，现行工作台用 bz_item |
| `fina_mainbz.segment_type` | core.business_segment | 0 | 未知 | 待核 |
| `fina_mainbz.ts_code` | core.business_segment | 445 | 间接 | 工作台/主营业务来源证券、报告期及修订校验；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `fina_mainbz.update_flag` | core.business_segment | 383 | 间接 | 工作台/主营业务来源证券、报告期及修订校验 |
| `fina_mainbz_vip.end_date` | core.business_segment | 0 | 未知 | 待核 |
| `fina_mainbz_vip.revenue` | core.business_segment | 0 | 未知 | 待核 |
| `fina_mainbz_vip.segment_name` | core.business_segment | 0 | 未知 | 待核 |
| `fina_mainbz_vip.segment_type` | core.business_segment | 0 | 未知 | 待核 |
| `fina_mainbz_vip.ts_code` | core.business_segment | 0 | 未知 | 待核 |
| `forecast.ann_date` | core.financial_forecast | 65 | 间接 | 工作台/预告详情补充、版本匹配 |
| `forecast.change_reason` | core.financial_forecast | 3 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast.end_date` | core.financial_forecast | 65 | 间接 | 工作台/预告详情补充、版本匹配 |
| `forecast.first_ann_date` | core.financial_forecast | 3 | 间接 | 工作台/预告详情补充、版本匹配 |
| `forecast.net_profit_max` | core.financial_forecast | 65 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast.net_profit_min` | core.financial_forecast | 65 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast.p_change_max` | core.financial_forecast | 65 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast.p_change_min` | core.financial_forecast | 65 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast.summary` | core.financial_forecast | 3 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast.ts_code` | core.financial_forecast | 65 | 间接 | 工作台/预告详情补充、版本匹配 |
| `forecast.type` | core.financial_forecast | 65 | 直接 | 工作台/公告与业绩预告明细 |
| `forecast_vip.ann_date` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.end_date` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.net_profit_max` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.net_profit_min` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.p_change_max` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.p_change_min` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.ts_code` | core.financial_forecast | 0 | 未知 | 待核 |
| `forecast_vip.type` | core.financial_forecast | 0 | 未知 | 待核 |
| `fund_daily.amount` | raw.source_payload | 9 | 直接 | 市场/ETF 收盘、涨跌与成交 |
| `fund_daily.close` | raw.source_payload / core.market_daily_quote.close（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 18 | 直接 | 市场/ETF 收盘、涨跌与成交；市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.high` | raw.source_payload / core.market_daily_quote.high（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 9 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.low` | raw.source_payload / core.market_daily_quote.low（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 9 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.open` | raw.source_payload / core.market_daily_quote.open（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 9 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.pct_chg` | raw.source_payload / core.market_daily_quote.pct_chg（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 18 | 直接 | 市场/ETF 收盘、涨跌与成交；市场总览/宽基 ETF 卡/etfQuotes[].change 涨跌幅；市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.pre_close` | raw.source_payload / core.market_daily_quote.pre_close（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 9 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.trade_date` | raw.source_payload / core.market_daily_quote.trade_date（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 18 | 直接 | 市场总览/宽基 ETF 卡/etfQuotes[].tradeDate 收盘交易日；市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.ts_code` | raw.source_payload / core.market_daily_quote.asset_code（market:etfs:资产代码；证券代码与配置身份校验，63 行） | 18 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `fund_daily.vol` | raw.source_payload | 9 | 间接 | 市场/ETF 成交量非负核验 |
| `fund_share.fd_share` | raw.source_payload | 9 | 直接 | 市场/ETF 份额；参与基金规模换算 |
| `fund_share.trade_date` | raw.source_payload | 9 | 间接 | 市场/ETF 份额证券与行情同日核验 |
| `fund_share.ts_code` | raw.source_payload | 9 | 间接 | 市场/ETF 份额证券与行情同日核验 |
| `fut_basic.delist_date` | raw.source_payload | 10 | 间接 | 市场/商品有效合约区间、报价和交易单位 |
| `fut_basic.exchange` | raw.source_payload | 10 | 间接 | 市场/商品月合约目录与交易所校验 |
| `fut_basic.fut_code` | raw.source_payload | 10 | 间接 | 市场/商品月合约目录与交易所校验 |
| `fut_basic.list_date` | raw.source_payload | 10 | 间接 | 市场/商品有效合约区间、报价和交易单位 |
| `fut_basic.name` | raw.source_payload | 10 | 间接 | 市场/商品目录整列参与请求指纹、回包字段集合和游标哈希复验；页面名称来自资产配置 |
| `fut_basic.per_unit` | raw.source_payload | 10 | 间接 | 市场/商品有效合约区间、报价和交易单位 |
| `fut_basic.quote_unit` | raw.source_payload | 10 | 间接 | 市场/商品有效合约区间、报价和交易单位 |
| `fut_basic.trade_unit` | raw.source_payload | 10 | 间接 | 市场/商品有效合约区间、报价和交易单位 |
| `fut_basic.ts_code` | raw.source_payload | 10 | 间接 | 市场/商品月合约目录与交易所校验 |
| `fut_daily.amount` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_daily.close` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_daily.high` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_daily.low` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_daily.oi` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_daily.open` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_daily.pre_close` | raw.source_payload | 33 | 间接 | 市场/商品主力日行情完整性校验 |
| `fut_daily.pre_settle` | raw.source_payload | 33 | 间接 | 市场/商品主力日行情完整性校验 |
| `fut_daily.settle` | raw.source_payload | 33 | 间接 | 市场/商品主力日行情完整性校验 |
| `fut_daily.trade_date` | raw.source_payload | 33 | 间接 | 市场/合约行情对应主力与日期 |
| `fut_daily.ts_code` | raw.source_payload | 33 | 间接 | 市场/合约行情对应主力与日期 |
| `fut_daily.vol` | raw.source_payload | 33 | 直接 | 市场/商品日 K，周月聚合 |
| `fut_mapping.mapping_ts_code` | raw.source_payload | 10 | 间接 | 市场/商品主力合约及换月 |
| `fut_mapping.trade_date` | raw.source_payload | 10 | 间接 | 市场/主力映射与开市日逐日对齐 |
| `fut_mapping.ts_code` | raw.source_payload | 10 | 间接 | 市场/主力映射与开市日逐日对齐 |
| `fut_trade_cal.cal_date` | raw.source_payload | 4 | 间接 | 市场/商品开市日选择 |
| `fut_trade_cal.exchange` | raw.source_payload | 4 | 间接 | 市场/商品开市日选择 |
| `fut_trade_cal.is_open` | raw.source_payload | 4 | 间接 | 市场/商品开市日选择 |
| `fx_daily.ask_close` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/离岸汇率报价方向、时点和身份 |
| `fx_daily.bid_close` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/离岸汇率报价方向、时点和身份 |
| `fx_daily.exchange` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/离岸汇率报价方向、时点和身份 |
| `fx_daily.trade_date` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/离岸汇率报价方向、时点和身份 |
| `fx_daily.ts_code` | raw.source_payload / 宏观观测按发布合同 | 4 | 间接 | 宏观/离岸汇率报价方向、时点和身份 |
| `income.admin_exp` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[admin_exp] |
| `income.ann_date` | core.income_statement / 财报专题 Raw | 170 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.assets_impair_loss` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[assets_impair_loss]；工作台/风险面板的报告筛选及计算 |
| `income.basic_eps` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[basic_eps]；工作台/估值与财务指标卡的报告取版；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.biz_tax_surchg` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[biz_tax_surchg] |
| `income.comp_type` | core.income_statement / 财报专题 Raw | 170 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.credit_impa_loss` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[credit_impa_loss]；工作台/风险面板的报告筛选及计算 |
| `income.diluted_eps` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[diluted_eps] |
| `income.ebit` | core.income_statement / 财报专题 Raw | 170 | 直接 | 工作台/财务报表/financial.statements.tables.rows[ebit]；工作台/比率分析/metrics.fields[ebit]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.ebitda` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[ebitda]；工作台/比率分析/metrics.fields[ebitda]（按别名顺序取首个有效值） |
| `income.end_date` | core.income_statement / 财报专题 Raw | 170 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.f_ann_date` | core.income_statement / 财报专题 Raw | 170 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.fin_exp` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[fin_exp] |
| `income.fin_exp_int_exp` | core.income_statement / 财报专题 Raw | 166 | 直接 | 工作台/财务报表/financial.statements.tables.rows[fin_exp_int_exp]；工作台/比率分析/metrics.fields[interestExpense]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.fin_exp_int_inc` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[fin_exp_int_inc] |
| `income.fv_value_chg_gain` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[fv_value_chg_gain]；工作台/风险面板的报告筛选及计算 |
| `income.income_tax` | core.income_statement / 财报专题 Raw | 166 | 直接 | 工作台/财务报表/financial.statements.tables.rows[income_tax]；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.invest_income` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[invest_income]；工作台/风险面板的报告筛选及计算 |
| `income.minority_gain` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[minority_gain] |
| `income.n_income` | core.income_statement / 财报专题 Raw | 166 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_income]；工作台/比率分析/metrics.fields[netProfit]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.n_income_attr_p` | core.income_statement / 财报专题 Raw | 170 | 直接 | 工作台/财务报表/financial.statements.tables.rows[n_income_attr_p]；工作台/估值与财务指标卡的报告取版；工作台/比率分析/metrics.fields[parentNetProfit]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/payoutRatio；选股/realMoneyIndex（FY、3/5/10 年） |
| `income.non_oper_exp` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[non_oper_exp]；工作台/风险面板的报告筛选及计算 |
| `income.non_oper_income` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[non_oper_income]；工作台/风险面板的报告筛选及计算 |
| `income.oper_cost` | core.income_statement / 财报专题 Raw | 170 | 直接 | 工作台/财务报表/financial.statements.tables.rows[oper_cost]；工作台/比率分析/metrics.fields[cost]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/cashPaymentRatio（FY、3/5/10 年） |
| `income.operate_profit` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[operate_profit]；工作台/比率分析/metrics.fields[operatingProfit]（按别名顺序取首个有效值） |
| `income.rd_exp` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[rd_exp] |
| `income.report_type` | core.income_statement / 财报专题 Raw | 170 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.revenue` | core.income_statement / 财报专题 Raw | 170 | 直接 | 工作台/财务报表/financial.statements.tables.rows[revenue]；工作台/比率分析/metrics.fields[revenue]（按别名顺序取首个有效值）；工作台/风险面板的报告筛选及计算；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/ocfToRevenue, cashCollectionRatio, freeCashFlowMarginFy（FY、3/5/10 年） |
| `income.sell_exp` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[sell_exp] |
| `income.t_compr_income` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[t_compr_income]；工作台/风险面板的报告筛选及计算 |
| `income.total_cogs` | core.income_statement / 财报专题 Raw | 100 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_cogs] |
| `income.total_profit` | core.income_statement / 财报专题 Raw | 166 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_profit]；工作台/比率分析/metrics.fields[totalProfit]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.total_revenue` | core.income_statement / 财报专题 Raw | 166 | 直接 | 工作台/财务报表/financial.statements.tables.rows[total_revenue]；工作台/比率分析/metrics.fields[revenue]（按别名顺序取首个有效值）；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.ts_code` | core.income_statement / 财报专题 Raw | 170 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income.update_flag` | core.income_statement / 财报专题 Raw | 170 | 间接 | 工作台/可见报告期、证券与修订取版；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `income_vip.ann_date` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.basic_eps` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.comp_type` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.ebit` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.end_date` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.f_ann_date` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.fin_exp_int_exp` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.income_tax` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.n_income` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.n_income_attr_p` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.oper_cost` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.report_type` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.revenue` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.total_profit` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.total_revenue` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.ts_code` | core.income_statement | 0 | 未知 | 待核 |
| `income_vip.update_flag` | core.income_statement | 0 | 未知 | 待核 |
| `index_basic.base_date` | raw.source_payload | 2 | 间接 | 组合/两项全收益基准旧目录的请求哈希与不可变原件校验；基准筛选不读这三个值 |
| `index_basic.desc` | raw.source_payload | 2 | 间接 | 组合/两项全收益基准旧目录的请求哈希与不可变原件校验；基准筛选不读这三个值 |
| `index_basic.exp_date` | raw.source_payload / raw.enterprise_target_observation | 3 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `index_basic.fullname` | raw.source_payload / raw.enterprise_target_observation | 3 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围；组合/全收益基准目录与目标名称 |
| `index_basic.list_date` | raw.source_payload | 2 | 间接 | 组合/两项全收益基准旧目录的请求哈希与不可变原件校验；基准筛选不读这三个值 |
| `index_basic.market` | raw.source_payload / raw.enterprise_target_observation | 3 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围；组合/全收益目录身份校验 |
| `index_basic.name` | raw.source_payload / raw.enterprise_target_observation | 3 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `index_basic.publisher` | raw.source_payload / raw.enterprise_target_observation | 3 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围；组合/全收益目录身份校验 |
| `index_basic.ts_code` | raw.source_payload / raw.enterprise_target_observation | 3 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围；组合/全收益基准目录与目标名称 |
| `index_classify.index_code` | core.classification | 5 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、市场/分类树与行业标签 |
| `index_classify.industry_code` | core.classification | 5 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、市场/分类树与行业标签 |
| `index_classify.industry_name` | core.classification | 5 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、市场/分类树与行业标签 |
| `index_classify.is_pub` | core.classification | 5 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `index_classify.level` | core.classification | 6 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、市场/分类树与行业标签 |
| `index_classify.parent_code` | core.classification | 5 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、市场/分类树与行业标签 |
| `index_classify.src` | core.classification | 5 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `index_daily.close` | raw.source_payload / core.market_daily_quote.close（market:indices:资产代码；证券代码与配置身份校验，21 行） | 5 | 直接 | 市场总览/全球指数卡/globalIndices[].value 收盘点位（两位小数）；市场/指数或 ETF 同日行情和产品校验；组合/基准回报 |
| `index_daily.high` | raw.source_payload / core.market_daily_quote.high（market:indices:资产代码；证券代码与配置身份校验，21 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_daily.low` | raw.source_payload / core.market_daily_quote.low（market:indices:资产代码；证券代码与配置身份校验，21 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_daily.open` | raw.source_payload / core.market_daily_quote.open（market:indices:资产代码；证券代码与配置身份校验，21 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_daily.pct_chg` | raw.source_payload / core.market_daily_quote.pct_chg（market:indices:资产代码；证券代码与配置身份校验，21 行） | 3 | 直接 | 市场总览/全球指数卡/globalIndices[].change 涨跌幅；市场/指数或 ETF 同日行情和产品校验 |
| `index_daily.pre_close` | raw.source_payload / core.market_daily_quote.pre_close（market:indices:资产代码；证券代码与配置身份校验，21 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_daily.trade_date` | raw.source_payload / core.market_daily_quote.trade_date（market:indices:资产代码；证券代码与配置身份校验，21 行） | 5 | 直接 | 市场总览/全球指数卡/globalIndices[].tradeDate 收盘交易日；市场/指数或 ETF 同日行情和产品校验 |
| `index_daily.ts_code` | raw.source_payload / core.market_daily_quote.asset_code（market:indices:资产代码；证券代码与配置身份校验，21 行） | 5 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_global.close` | raw.source_payload / core.market_daily_quote.close（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 直接 | 市场总览/全球指数卡/globalIndices[].value 收盘点位（两位小数）；市场/指数或 ETF 同日行情和产品校验 |
| `index_global.high` | raw.source_payload / core.market_daily_quote.high（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_global.low` | raw.source_payload / core.market_daily_quote.low（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_global.open` | raw.source_payload / core.market_daily_quote.open（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_global.pct_chg` | raw.source_payload / core.market_daily_quote.pct_chg（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 直接 | 市场总览/全球指数卡/globalIndices[].change 涨跌幅；市场/指数或 ETF 同日行情和产品校验 |
| `index_global.pre_close` | raw.source_payload / core.market_daily_quote.pre_close（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_global.trade_date` | raw.source_payload / core.market_daily_quote.trade_date（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 直接 | 市场总览/全球指数卡/globalIndices[].tradeDate 收盘交易日；市场/指数或 ETF 同日行情和产品校验 |
| `index_global.ts_code` | raw.source_payload / core.market_daily_quote.asset_code（market:indices:资产代码；证券代码与配置身份校验，30 行） | 3 | 间接 | 市场/指数或 ETF 同日行情和产品校验 |
| `index_member_all.in_date` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.is_new` | core.classification_member | 100 | 间接 | 选股、市场/分类数据集当前与历史有效区间 |
| `index_member_all.l1_code` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.l1_name` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.l2_code` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.l2_name` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.l3_code` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.l3_name` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.name` | core.classification_member | 100 | 间接 | 市场与选股/行业成员原件全包哈希与合同指纹；页面显示证券名来自证券主表 |
| `index_member_all.out_date` | core.classification_member | 100 | 间接 | 选股、市场/证券有效行业归属与板块聚合 |
| `index_member_all.ts_code` | core.classification_member | 100 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股、市场/证券有效行业归属与板块聚合 |
| `index_weight.con_code` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `index_weight.index_code` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `index_weight.trade_date` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `index_weight.weight` | 待取得原件 | 0 | 间接 | 宏观/企业指数或 ETF 候选证据校验；候选不等于页面已采用的成员范围 |
| `margin.exchange_id` | raw.source_payload / 宏观观测按发布合同 | 6 | 间接 | 宏观/融资余额分交易所核验与汇总 |
| `margin.rzye` | raw.source_payload / 宏观观测按发布合同 | 6 | 间接 | 宏观/融资余额分交易所核验与汇总 |
| `margin.trade_date` | raw.source_payload / 宏观观测按发布合同 | 6 | 间接 | 宏观/融资余额分交易所核验与汇总 |
| `repo_daily.repo_maturity` | raw.source_payload / 宏观观测按发布合同 | 124 | 间接 | 宏观/DR007 利率与品种、日期校验 |
| `repo_daily.trade_date` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.observed_at（系列 cn.rate.dr007 的 Tushare 报告期或数值，经转换与去重/修订，2504 行） | 124 | 间接 | 宏观/DR007 利率与品种、日期校验；宏观/指标序列 cn.rate.dr007（供应商配置，实际来源按版本） |
| `repo_daily.ts_code` | raw.source_payload / 宏观观测按发布合同 | 124 | 间接 | 宏观/DR007 利率与品种、日期校验 |
| `repo_daily.weight` | raw.source_payload / 宏观观测按发布合同 / core.macro_observation.value（系列 cn.rate.dr007 的 Tushare 报告期或数值，经转换与去重/修订，2504 行） | 124 | 间接 | 宏观/DR007 利率与品种、日期校验；宏观/指标序列 cn.rate.dr007（供应商配置，实际来源按版本） |
| `repurchase.amount` | core.repurchase | 62 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/资本回报与股权筹资比 |
| `repurchase.ann_date` | core.repurchase | 62 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `repurchase.exp_date` | core.repurchase | 62 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `repurchase.proc` | core.repurchase | 62 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；选股/资本回报与股权筹资比 |
| `repurchase.ts_code` | core.repurchase | 62 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `repurchase.vol` | core.repurchase | 62 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `share_float.ann_date` | core.share_unlock | 65 | 间接 | 股票发布/公司行为可见性与来源签名；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `share_float.float_date` | core.share_unlock | 65 | 间接 | 股票发布/公司行为可见性与来源签名；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `share_float.float_share` | core.share_unlock | 65 | 间接 | 股票发布/公司行为可见性与来源签名；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `share_float.ts_code` | core.share_unlock | 65 | 间接 | 股票发布/公司行为可见性与来源签名；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `share_float.unlock_date` | core.share_unlock | 0 | 仅历史 | 旧宽合同/新请求已删除，历史回放保留；当次历史没有返回该键 |
| `stk_holdernumber.ann_date` | core.shareholder_count | 63 | 间接 | 工作台/股东户数期末及公告取版 |
| `stk_holdernumber.end_date` | core.shareholder_count | 63 | 间接 | 工作台/股东户数期末及公告取版 |
| `stk_holdernumber.holder_num` | core.shareholder_count | 63 | 直接 | 工作台/股东户数 |
| `stk_holdernumber.ts_code` | core.shareholder_count | 63 | 间接 | 工作台/股东户数期末及公告取版 |
| `stock_basic.area` | core.security | 50 | 直接 | 市场/地区热力分类；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `stock_basic.exchange` | core.security | 8 | 间接 | 股票任务/上市日期与交易所限定预期交易日；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `stock_basic.list_date` | core.security | 8 | 间接 | 股票任务/上市日期与交易所限定预期交易日；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `stock_basic.list_status` | core.security | 8 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查）；股票发布/证券可见和退市校验 |
| `stock_basic.market` | core.security | 8 | 间接 | 股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `stock_basic.name` | core.security | 8 | 直接 | 搜索、选股、工作台、市场、组合/证券代码及名称；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `stock_basic.symbol` | core.security | 8 | 间接 | 搜索、组合/证券简称拼音备选、证券编号；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `stock_basic.ts_code` | core.security | 58 | 直接 | 搜索、选股、工作台、市场、组合/证券代码及名称；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `sz_daily_info.count` | raw.source_payload | 1 | 间接 | 宏观/深市统计样本合同与数据类型；单位待验，未晋升正式序列 |
| `sz_daily_info.float_mv` | raw.source_payload | 1 | 间接 | 宏观/深市统计样本合同与数据类型；单位待验，未晋升正式序列 |
| `sz_daily_info.trade_date` | raw.source_payload | 1 | 间接 | 宏观/深市统计样本合同与数据类型；单位待验，未晋升正式序列 |
| `sz_daily_info.ts_code` | raw.source_payload | 1 | 间接 | 宏观/深市统计样本合同与数据类型；单位待验，未晋升正式序列 |
| `top10_holders.ann_date` | core.top_holder | 62 | 间接 | 选股、工作台/按证券与可见日期取股东名册 |
| `top10_holders.end_date` | core.top_holder | 62 | 间接 | 选股、工作台/按证券与可见日期取股东名册 |
| `top10_holders.hold_amount` | core.top_holder | 62 | 间接 | 选股/institutionHolding、institutionHoldingChange |
| `top10_holders.holder_name` | core.top_holder | 62 | 间接 | 选股/institutionHolding、institutionHoldingChange |
| `top10_holders.holder_type` | core.top_holder | 62 | 间接 | 选股/institutionHolding、institutionHoldingChange |
| `top10_holders.ts_code` | core.top_holder | 62 | 间接 | 选股、工作台/按证券与可见日期取股东名册 |
| `trade_cal.cal_date` | core.trade_calendar | 12 | 间接 | 股票与市场/发现业务日并对齐行情窗口；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `trade_cal.exchange` | core.trade_calendar | 12 | 间接 | 股票与市场/发现业务日并对齐行情窗口；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `trade_cal.is_open` | core.trade_calendar | 12 | 间接 | 股票与市场/发现业务日并对齐行情窗口；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `trade_cal.pretrade_date` | core.trade_calendar | 11 | 间接 | 宏观/交易所汇总的前一交易日证据；股票发布/可见来源签名（真实快照内存在该字段；供构建前后冲突检查） |
| `us_tycr.date` | 待取得原件 / core.macro_observation.observed_at（系列 us.rate.treasury_10y 的 Tushare 报告期或数值，经转换与去重/修订，0 行） | 0 | 间接 | 宏观/指标序列 us.rate.treasury_10y（供应商配置，实际来源按版本） |
| `us_tycr.y10` | 待取得原件 / core.macro_observation.value（系列 us.rate.treasury_10y 的 Tushare 报告期或数值，经转换与去重/修订，0 行） | 0 | 间接 | 宏观/指标序列 us.rate.treasury_10y（供应商配置，实际来源按版本） |
