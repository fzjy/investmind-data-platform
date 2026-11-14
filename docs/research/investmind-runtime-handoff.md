# 底座本机配置与开工备份交接

2026-11-14 核验。源仓库存有有效开工备份和运行配置。在获准向本机独立仓交付后，已创建目标配置和该备份包并完成真实 API 验收。源文件均保留。

## 配置

源 `.env` 有 25 个显式键，21 个与底座运行模板的键匹配。4 个网站专属键为 `NEXT_PUBLIC_API_BASE_URL`、`DEV_PORT`、`PORT`、`DANG_LOCAL_FRONTEND_PORT`。底座服务端口单独由 `DANG_BACKEND_PORT` 决定。源端实际调用 `Settings.from_env()` 可以解析数据库连接和两个供应商凭据；没有调用供应商。源端没有单设读连接，设置模型将读连接回落至主连接。本结论只描述配置解析，未验证数据库角色为只读角色。

独立仓 `E:/code/investmind-data-platform/.env` 由 21 个数据侧配置项新建，未覆盖既有文件。已用 `git check-ignore -v` 确认它命中 `.env*`，且 `git ls-files` 没有跟踪记录。包内 `database.dump`、`manifest.json`、`verification.json` 三条目标路径全部命中 `backend/backups/`，均未被跟踪。Docker 构建上下文同时排除了这两类文件。

## 源端恢复点

源目录为 `backend/backups/20261114T124926Z-30c2e10cdc2d4fb8b0d3723194acb643`。目标使用独立仓同一相对目录。源、目标均逐文件计算了完整 SHA-256，结果相同。

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| database.dump | 424481381 | `0f012b943e8771d5aeb8e2d7b8e9caa94bc1acd5c9d251f3812abeb3ee4f2b23` |
| manifest.json | 32931 | `5c33ab88a7eaed3e5433d0b8f61c44eefe2b9718833ad64a9d29f8d7af74efb7` |
| verification.json | 637 | `ab19c1ba7bb330298edaeb6d54548d861938a5253cd4941901621b9d2a6c2876` |

回执的归档摘要与大小和清单一致，回执内的清单摘要也与文件一致。此前隔离恢复为 `completed / restore_verified`，66 张表，异常数 0。本次复核归档与恢复回执的完整绑定；目标使用既有文档化的隔离恢复证据，未再启动一次数据库恢复。

## 目标验收

目标配置文件和包目录均为新建，写入前校验 Git 忽略、未被跟踪，限制 Windows ACL 仅当前用户和 SYSTEM 后落盘。归档先整包校验再改名交付。配置原值、连接串、令牌和业务载荷均未进入 Git。Docker 配置模板不计入运行验收。

独立仓的真实 `Settings.from_env()` 验证 21 项与来源一致、两个供应商凭据可加载、读连接由主连接派生，新闻配置、状态和导入路径已解析到独立仓。使用现有 Python 依赖加载独立仓模块，关闭新闻后台，实测 `GET /health` 返回 ready，`GET /api/v1/portfolio` 返回 51 项资产，业务日 2026-10-21，发布版本 `20261021-690705b397d4b878`。另以只读事务直接查询发布指针，核对接口日期及版本。独立仓备份状态函数按当前连接目标识别该包为 `ready / restore_verified`，66 张表、异常 0。未调用供应商、启停既有服务或开启调度。
