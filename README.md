# InvestMind 数据底座

公共金融数据的 PostgreSQL 数据库、采集任务和 FastAPI 服务。选股接口见 [字段目录](docs/platform-api.md)。本仓库处于离线拆分验收阶段。

在仓库根目录复制 .env.example 为 .env，根据部署环境配置数据库连接与供应商密钥。独立容器构建及配置检查命令如下。

~~~sh
docker build -f backend/Dockerfile -t investmind-data-platform .
docker compose -f docker-compose.backend.yml config --quiet
~~~

Compose 的 postgres、migrate、bootstrap 和 api 提供完整的初始化流程。scheduler 在 scheduler profile 下显式启用。迁移期间先对照隔离库，切换定时任务时只保留一个执行方。用户资产由各消费项目独立管理。

2026-11-15 离线交接后，默认 API 只提供读取服务。部署时在 `scheduler` profile 中由单个 `news-worker` 运行 `python -m app.cli.main watch-news`，当前队列、游标和交付状态持久化在 PostgreSQL；兼容迁入旧 SQLite 的目录默认为底座的 `data/news-collector/`。本机纯 Python 部署使用同一命令。启用这一常驻进程时保留同一状态目录和数据库，避免同时运行另一个 watch-news。日、周调度仍由独立 scheduler 负责。本次仅验证配置与受控生命周期，未启用真实采集。
