# InvestMind 数据底座

公共金融数据的 PostgreSQL 数据库、采集任务和 FastAPI 服务。选股接口见 [字段目录](docs/platform-api.md)。本仓库处于离线拆分验收阶段。

在仓库根目录复制 .env.example 为 .env，根据部署环境配置数据库连接与供应商密钥。独立容器构建及配置检查命令如下。

~~~sh
docker build -f backend/Dockerfile -t investmind-data-platform .
docker compose -f docker-compose.backend.yml config --quiet
~~~

Compose 的 postgres、migrate、bootstrap 和 api 提供完整的初始化流程。scheduler 在 scheduler profile 下显式启用。迁移期间先对照隔离库，切换定时任务时只保留一个执行方。用户资产由各消费项目独立管理。
