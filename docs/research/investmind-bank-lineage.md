# InvestMind 银行选股数据溯源

2026-11-14 用只读 PostgreSQL 事务读取当期选股指针、原料和页面数据。截止日为 2026-10-21，版本为 `20261021-1c01ff6eb0b7d78a`。发布池 50 只中，`majorIndustry=银行` 有平安银行、招商银行、工商银行 3 只。以下覆盖率以这 3 只为分母。

## 来源与使用位置

当前请求路径是 InvestMind 选股页 → `POST /api/v1/stocks/screener` 的 `bank` 参数 → `mart.published_snapshot` 的 `screener` → `project_bank` → 每只证券的 `bankEvaluation`。服务端先在银行全集计算字段覆盖，再施加用户选择的板块、排除项、行业细分及银行条件。在 `fields` 中有值才参与对应条件的比较。每个条件均服务于 InvestMind 选股页银行模式的筛选区及评分详情；下表另列其余消费者。评分和用户权重按请求重算。

| 条件字段 | 默认条件 | 有值 / 命中 | 原料、转换和额外消费者 |
| --- | --- | --- | --- |
| `pb` | <1.2 倍 | 3 / 3 | Tushare `daily_basic.pb` → `core.daily_basic.pb` → 同日列表 `pb` → 银行筛选及 PB 分量。必须为正值 |
| `pbPercentile10y` | <40% | 0 / 0 | 普通列表的同名研究字段逐只显示 2016-10-21 至 2026-10-21 缺完整交易日历；银行投影也未读取该字段。依赖上市日期、交易日历、全天停牌证据、逐日有效 PB |
| `dividendYield` | >4% | 3 / 2 | Tushare `dividend` 原件及 `core.dividend` → 按财年裁决实付事件 → FY 税前实付每股分红 ÷ `core.daily_price.close` ×100 → `workbenchFiscalDividendYield` → `researchValues.dividendYield` |
| `expectedDividendYield` | >4% | 3 / 2 | 已付分红、可见同期及全年利润和基本 EPS、同日股本及收盘价 → 季节性外推、加权派息 → `workbenchAnnualDividendYield` → `researchValues.expectedDividendYield` |
| `bankRevisedExpectedDividendYield` | >5% | 0 / 0 | 缺修正规则、预测期间、预期 DPS 与价格基准。也是修正预期列与股息评分 `revisedYield` 所需概念 |
| `netInterestMargin` | >1.8% | 0 / 0 | 净息差列及 `nim` 分量需要本期、上年末同口径净息差。候选银行财报键 `bs.nim` |
| `bankRevisedExpectedGrowth` | >3% | 0 / 0 | 缺版本化修正规则和可核准预测，另用于修正预期列及股息评分 `revisedGrowth` |
| `roe` | >10% | 3 / 0 | Tushare `fina_indicator.roe` → `core.financial_indicator.data.roe` → 截止日最近可见报告 → 快照 `roe`。本期三家均为半年报 |
| `nplRatio` | <1.5% | 0 / 0 | 资产质量列缺公司级不良率。候选键 `bs.npl_tlaatc_r` |
| `provisionCoverage` | >250% | 0 / 0 | 资产质量列及 `provision` 分量缺本期与上年末拨备覆盖率。候选键 `bs.llr_npl_r` |
| `loanDepositRatio` | <90% | 0 / 0 | 资本 / 贷存列缺公司级贷存比。候选键 `m.laatc_cd_r` |
| `coreTier1CapitalAdequacyRatio` | >8% | 0 / 0 | 资本 / 贷存列缺公司级核心一级资本充足率。候选键 `bs.ct1car` |
| `bankDefensiveScore` | >6 分 | 0 / 0 | 缺经核准的来源表防守分及其版本 |
| `bankOffensiveScore` | >6 分 | 0 / 0 | 缺经核准的来源表进攻分及其版本 |
| `dilutionEventCount3y` | ≤0 次 | 0 / 0 | 缺增发、配股、转股等可完整核对的事件，包括完成日期、去重键和三年窗口 |

公司银行财报候选接口此前仅核对过公开说明。当前底座没有经验证的同口径输入、单位、披露修订及批量采集合同。另有展示项 `ahDiscount`，三家均未知。计算需要同日 A、H 价格、证券配对及 CNY/HKD 汇率，按 `(H价 × 汇率 ÷ A价 − 1) × 100%` 表示。A/H 没有出现在这 15 个条件中。候选字段来自冻结范围内的旧网站研究文档 `docs/research/bank-source-audit-2026-11-07.md`。

## 三家样本的数值复算

| 股票 | PB（倍） | 报告 ROE（%） | FY 已派股息率（%） | 本年度预期股息率（%） |
| --- | ---: | ---: | ---: | ---: |
| 000001.SZ 平安银行 | 0.4729 | 4.6746 | 5.223488 | 6.191119 |
| 600036.SH 招商银行 | 0.9312 | 5.8402 | 4.768212 | 5.004945 |
| 601398.SH 工商银行 | 0.7601 | 4.0527 | 3.672189 | 3.834908 |

PB、ROE 逐只比对到对应 Core 行 ID。两种股息率复用正式的派息事件裁决和财报修订选择，然后单独实现计算，与接口逐项比较，绝对误差容限为 0.00000001。FY 对应 2025 财年。三家均以 2026 中报累计归母净利为锚，用前五年同期利润占全年利润的合格比重均值估算全年归母净利。两年实付 DPS ÷ 各自年度基本 EPS 得到派息率，2024、2025 年权重为 3 和 5；预测全年利润 ÷ 同日总股本 × 加权派息率 ÷ 收盘价 ×100 得出预期股息率。原料来自 `core.income_statement`、`core.daily_basic`、`core.daily_price` 及分红裁决，年度基本 EPS 缺失时另读 `core.financial_indicator`。机器结果保存了逐只股价、股本、已派 DPS、预测利润及加权派息率。

## 九项评分输入与五个模板

| 分量 | 评分输入及公式 | 三家实测 |
| --- | --- | --- |
| 股息 | `revisedYield + revisedGrowth / divisor` | 0/3，两个修正预期输入均为空 |
| PB | 正 PB 的倒数；除数为 3 且有 `offensivePbScore` 时采用该输入 | 3/3，进攻专用 PB 分为空，五个模板均走倒数 |
| 大行 | `largeBankScore` 可取 0、0.5、1 | 0/3，缺经核准的银行分级 |
| 息差变化 | `sign(nim − yearEndNim)` | 0/3，缺本期与上年末同口径数据 |
| 拨备变化 | `min((provision − yearEndProvision) / 10, 1)` | 0/3，缺本期与上年末同口径数据 |

模板共用算法 `rea-bank-score-20261020`。分量 × 权重 /100 得到加权分，任意启用项缺值时总分未知。表内权重顺序为股息、PB、大行、息差、拨备。

| 模板 | 除数 | 权重（%） | 可复算的加权分 / 完整总分 |
| --- | ---: | --- | --- |
| 防守型 | 5 | 100 / 100 / 100 / 100 / 100 | PB 3/3，总分 0/3 |
| 攻防均衡 | 4 | 100 / 100 / 100 / 100 / 100 | PB 3/3，总分 0/3 |
| 进攻型 | 3 | 100 / 100 / 100 / 100 / 100 | PB 3/3，总分 0/3 |
| 质量稳健 | 5 | 80 / 80 / 80 / 140 / 140 | PB 3/3，总分 0/3 |
| 股息估值 | 5 | 130 / 130 / 100 / 80 / 80 | PB 3/3，总分 0/3 |

对 15 个实际默认条件各执行一次独立查询，对 5 个模板各执行一次按分数降序的查询。15 个银行×模板的 PB 加权分均与独立计算一致，全部总分为空时按代码排序。15 个字段中只有 4 个在三家有值，另外 11 个共 33 个空值均使单独的对应条件返回空集。拆分时应把当前实测覆盖、十年估值日历缺口及银行专项供给缺口随合同转移。旧来源分字段与请求时计算的模板总分分别登记。

从有后端依赖和只读数据库连接的仓库根目录运行 `python -I scripts/audit-bank-lineage.py` 可从当时的当前指针重放，需本机只读开发库。脚本检查事务只读属性。测试不调用供应商，不采集或发布数据。[复核脚本](../../scripts/audit-bank-lineage.py) · [逐项结果](investmind-bank-lineage.json)
