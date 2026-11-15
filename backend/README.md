# Python 数据服务开发说明

本目录是 InvestMind 数据底座的 Python 服务包，包含 API、数据采集与处理、命令行入口、调度任务和数据库迁移。整个仓库不包含前端。

项目安装、数据库初始化、启动、日常更新、新闻采集、备份恢复及升级统一见[本机手册](../docs/local-operations.md)、[服务器手册](../docs/server-operations.md)；项目定位和全部文档入口见[根 README](../README.md)。

## 开发环境

要求 Python 3.12 或以上版本。创建并激活虚拟环境后，从**仓库根目录**安装两个本地包：

```sh
python -m pip install -e ".[dev]" -e ./packages/news-collector
```

根 `.env` 用于本机运行配置，模板为 [`.env.example`](../.env.example)。查看 CLI 和迁移版本入口：

```sh
python -m app.cli.main --help
python -m alembic -c backend/alembic.ini heads
```

<a id="alembic-settings"></a>

### Alembic 配置项

[alembic.ini](alembic.ini) 由 Alembic 按系统编码读取。文件内使用英文注释，下表列出对应的中文说明。

| 配置节 | 配置项 | 用途 |
| --- | --- | --- |
| `alembic` | `script_location`、`prepend_sys_path` | 前者相对于 INI 所在位置定位迁移目录，后者把后端目录加入 Python 模块搜索路径。 |
| `alembic` | `path_separator` | 选择如何分隔路径列表。`os` 表示使用当前系统的路径分隔符。 |
| `alembic` | `sqlalchemy.url` | 迁移备用连接串。运行环境已提供 `DANG_DATABASE_URL` 时优先使用环境中的连接。 |
| `loggers`、`handlers`、`formatters` | `keys` | 分别列出要注册的日志记录器、输出处理器和格式器，名称对应后续配置节。 |
| `logger_root`、`logger_sqlalchemy`、`logger_alembic` | `level`、`handlers`、`qualname` | `level` 设置最低日志级别，`handlers` 指定输出处理器，子记录器留空时沿用上级。`qualname` 指定要匹配的 Python 日志记录器名称。 |
| `handler_console` | `class`、`args`、`level`、`formatter` | 使用流处理器向标准错误输出日志。`level=NOTSET` 接收记录器传来的所有级别，`formatter` 选择输出格式。 |
| `formatter_generic` | `format` | 指定每行日志中级别、记录器名称和消息的排列顺序。 |

## 代码导航

| 路径 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 应用与路由装配，默认只读，不启动新闻采集 |
| `app/cli/` | 初始化、更新、重建、状态检查、备份与升级命令 |
| `app/stocks/` | 选股器、工作台、同业研究和股票指标计算 |
| `app/market/`、`app/macro/` | 市场与宏观数据供给和发布 |
| `app/content/`、`app/portfolio/`、`app/search/` | 新闻公告、组合公共输入和证券搜索 |
| `app/connectors/` | 外部数据源适配器 |
| `app/db/`、`app/shared/` | 数据模型、快照仓库与公共服务 |
| `app/jobs/` | 独立常驻调度入口 |
| `migrations/`、`alembic.ini` | 数据库结构迁移 |
| [仓库根配置目录](../config/) | 按业务领域维护的运行配置和历史目录，也是构建安装包的数据来源 |
| `tests/` | 离线隔离测试与显式 PostgreSQL 验收 |

## 验证

从仓库根目录执行离线测试，运行器隔离用户 `.env`、业务数据库和真实供应商 HTTP：

```sh
python -I backend/tests/run_isolated.py pytest backend/tests -q
```

需要一次性 PostgreSQL 容器的用例默认不运行；显式容器验收和测试归属见[离线测试说明](../docs/research/platform-test-ownership.md)。新闻包自身的边界和测试见[新闻采集包说明](../packages/news-collector/README.md)。
