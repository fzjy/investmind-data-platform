# 网站展示配置与底座运行配置

本文记录拆分时的跨仓库核验；其中 `src/`、`website-services/`、npm 和 Next.js 均指外部 InvestMind 网站仓库，不是本底座的目录或启动要求。当前底座运行方式见[本机手册](local-operations.md)、[服务器手册](server-operations.md)。

清单 4.7 将三组原先混用的 JSON 按实际读取方拆分。底座镜像继续从仓库根目录 `config/` 读取；网站组件只导入 `src/config/`。数值相同且两端都要解释的字段归公开数据合同维护。两个仓库可以独立构建。

| 配置 | 底座供给范围 | 网站读取范围 |
| --- | --- | --- |
| 财务指标 | `config/stocks/financial-indicators.json` 保留公告、业绩预告、财报、主营、估值的采集策略，以及宏观历史桥接、研究计算规则及银行评分。银行字段保留 ID、默认比较符及值供审计重放，保留算法版本、分母、权重归一化及五个预设的数值 | `src/config/financial-indicators.json` 包含 4 项杜邦、4 项营运、93 个报表字段与 6 个分组说明。`src/config/bank-screener.json` 包含网站交互的参数、标签、单位、条件和预设。选股页不会因为读取银行规则而导入整份说明或底座运行策略 |
| 组合 | `config/portfolio/portfolio.json` 仅保留 routine、dividendEvidence、referenceEvidence。底座交付证券行情、基准和分红数据 | `src/config/portfolio.json` 保留网站组合研究、比较、风险估算、优化和交互设置。私有持仓分析服务另有 `website-services/portfolio-analysis/settings.json`，拥有服务端的 annualizationDays=252、apiMaxAssets=100。迁移期网站仓库旧 Python 接口沿用同值 |
| 财务比率 | `config/stocks/stock-ratio-catalog.json` 保留 32 个实际发布的报表行映射，每项只含 id、label、category、table、keys，供发布器输出同名指标字段 | `src/config/stock-ratio-catalog.json` 保留 44 个选择项、111 对比率规则，支持方向、默认选择、派生计算和旧版报表。32 个基本项的 statementKind 与 rowAliases 用以查找 API 已返回的利润/资产负债/现金流表及行名。底座保留的 table/keys 指向发布时使用的会计数据 |

**兼容与已验范围。** 网站从原有 JSON 中摘出显示和本地计算所需的值，后端发布字段、公式及现有阈值均沿用原口径。网站仓库迁移期根目录三份 JSON 仍供旧 Python 副本和审计脚本读取。财务和比率文件与独立底座一致；组合文件额外保留旧私有分析入口所需的两个参数。独立底座没有引入网站服务依赖。移除旧 Python 副本时，应同时复核这些根文件及网站端旧审计脚本的去留。

**构建验证。** 网站股票/工作台 273 项、组合 248 项、独立私有分析服务 4 项、网站 Python 迁移副本 152 项和独立底座 99 项均通过。两组 Python 测试均使用离线隔离环境，业务库、真实 HTTP 访问违规为 0。TypeScript 类型检查及 Next.js 16.2.1 正式生产构建通过。核验了生成的 27 个客户端和 84 个服务端 JS 块，没有包含公告索引、业绩预告详情、估值或主营供给、宏观历史桥接、分红及参考资料预算等底座配置标识；三个客户端块包含所需展示配置。

Next.js standalone 会自动复制构建时加载的根 `.env` 和 `.env.production`。网站的 `npm run build` 现会清除这两个构建输出副本，网站运行设置由部署环境提供。最终 standalone 目录没有这两个文件，也没有后端或根配置目录。该检查覆盖本地生产构建，未启动部署服务或读取凭据值。
