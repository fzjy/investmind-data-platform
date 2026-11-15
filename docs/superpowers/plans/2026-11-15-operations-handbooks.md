# 两本运行手册实施计划

按[已确认的设计](../specs/2026-11-15-operations-handbooks-design.md)在当前工作区实施。技术依据是 Python 3.12+ 的 CLI、Alembic、PostgreSQL 17 和仓库内 Compose 声明。

1. 在 `docs/local-operations.md` 整理本机完整流程，保留命令解释和受控维护约束。末章记录停写、最终备份、验证和交接。
2. 在 `docs/server-operations.md` 分别编写空库部署与接收备份的流程，然后汇入正常启停、导入、采集、备份和升级。从 Compose 核对挂载、环境变量和服务依赖。
3. 更新首页、包说明、专题文档和既有文档合同测试，检查迁移链接、全部文档入口与四种使用场景，移除旧手册。使用隔离运行器验证受影响测试。
