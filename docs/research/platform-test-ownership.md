# 离线测试归属

独立底座通过 `python -I backend/tests/run_isolated.py pytest backend/tests -q` 验证发布与采集代码。安装步骤见[底座运行手册](../../backend/README.md)。运行器从本仓读取 app、测试模块和 packages/news-collector/src，隔离业务数据库、真实供应商 HTTP、个人 .env 与默认 PostgreSQL 容器。Compose 合同使用受控示例配置执行 config 解析。

2026-11-15 对原五组拆分测试实跑，23 项失败、6 项通过。以下变更将仍适用于底座的断言绑定至可执行入口。原测试在网站仓库提交 `701befd78024c6957dd38dde4a5bcb016f8b259e` 的 `backend/tests/` 下完整保存。保留该代码版本只为回溯，独立测试不读取网站仓库或其 Git 历史。

| 原测试 | 底座验收 | 由网站历史保留 |
| --- | --- | --- |
| test_fullstack_compose_contract.py | test_platform_file_boundaries.py 验证 .env、备份、缓存和本地密钥的 Git/镜像边界。test_backend_compose_contract.py 直接渲染底座 Compose 并检查 bootstrap、API、scheduler、环境参数、启动顺序及卷隔离 | Next.js、Node、浏览器代理、前端容器、npm 脚本和原组合栈 |
| test_alembic_command.py | 运行 Alembic heads，对照底座手册的迁移、版本及 CLI 入口 | npm 跨平台命令别名 |
| test_section86_documentation_contract.py | test_content_import_documentation.py 核对 JSON/JSONL 导入、版权、宏观补跑、发布步骤和配置预算 | 开发进展表、原网站登记章节 |
| test_backup_documentation_contract.py | 检查归档的 Git 忽略、三个成员文件、隔离恢复及读取状态的实际命令 | 旧网站 Git 提交中固定的 8.7 表格格式 |
| test_macro_documentation_contract.py | 检查宏观源与 FRED 说明、独立 Python 命令、受控 Compose 转发与 FRED 凭据的服务边界 | ESLint 和网页工程运行命令 |

全量基线还发现 test_reference_history.py 的一个用例导入已迁回网站的私有组合分析服务。底座保留公共指数的交易日完整性、日期可见性和发布合同测试，指数只能作为参照、不能作为用户持仓的行为归网站私有服务。

独立底座的宏观 exporter 保留在 backend/tests/e2e，用于显式生成跨语言受控输出；网站的 18 个测试文件使用本仓随附的 24 份 gzip JSON。网站不会执行底座的 Python。

现有页面 DTO 的字段与数据类型仍由底座测试覆盖，避免损害其他数据消费者依赖的公开合同。PostgreSQL 容器、生产调度和真实供应商取数的验收不计入默认离线测试。

## 离线验收回执

2026-11-15，独立底座使用自己的 `backend/.venv` 执行 `python -I backend/tests/run_isolated.py pytest backend/tests -q`。变更前全套有 7400 项通过、24 项失败、190 项按环境条件跳过。迁移后收集 7600 项，其中 7410 项通过、190 项按环境条件跳过，0 失败、0 错误、1 项警告，耗时 956.30 秒。隔离运行器报告真实 HTTP 或业务库访问违规数为 0，登记的临时 PostgreSQL 实例为 0。独立测试提交为 `8280f94`。

网站合同提交 `54f4896` 包含 24 份宏观公开合同样本和原 18 个测试文件。网站宏观根目录 519 项、嵌套审计 18 项、私有组合兼容 5 项均通过。
