# 数据审计与网站联测

本仓的 Python 脚本使用本仓环境和数据库，以公共快照、HTTP 或标准输入同 InvestMind 网站的测试驱动交互。网站的组合配置只交给网站私有分析服务。所有例子从底座根目录运行，输出路径应是新回执的位置。

```powershell
$platformPython = 'backend/.venv/Scripts/python.exe'
$website = (Resolve-Path ../ai-website-cloner-danginvest).Path
& $platformPython -I scripts/audit-security-search.py --website $website --output <新回执>
& $platformPython -I scripts/audit-portfolio-split.py --website $website --output <新回执>
& $platformPython -I scripts/audit-screener-browser.py --website $website <新回执>
```

浏览器联测要求网站已生产构建。增加 `--workbench` 或 `--market` 可分别核对工作台和市场。可通过环境变量 `PLAYWRIGHT_MODULE`、`CHROME_EXECUTABLE` 指定浏览器驱动。证券搜索可省略 `--website` 单独检查底座路由。

从网站根目录执行下列数据样本重放。独立底座负责提取公共输入；Node 使用真实页面计算模块并验证逐项结果。两仓不在同级时，请修改第一行。

```powershell
$platformRoot = (Resolve-Path ../investmind-data-platform).Path
$python = Join-Path $platformRoot 'backend/.venv/Scripts/python.exe'
$extract = Join-Path $platformRoot 'scripts/audit-website-projection-inputs.py'
& $python -I $extract workbenches | node scripts/audit-workbench-scenarios.mjs
& $python -I $extract workbenches | node scripts/audit-workbench-ratio.mjs
& $python -I $extract statements | node scripts/audit-statement-projection.mjs
& $python -I $extract macro-cards | node scripts/audit-macro-website-cards.mjs
```

底座接收脚本的提交为 `7747d3f`。拆分前同仓脚本保存在网站 Git 提交 `701befd78024c6957dd38dde4a5bcb016f8b259e`。2026-11-15 已分别完成真实浏览器三个场景、真实搜索服务、私有组合服务、工作台及宏观样本的双仓重放。版本、数据日期与计数见网站仓 `docs/research/investmind-audit-handoff.json`。
