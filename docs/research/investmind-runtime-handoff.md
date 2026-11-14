# 底座本机配置与开工备份交接

2026-11-14 核验。源仓库存有有效开工备份和运行配置。独立仓的本机配置和该备份包均未交付，独立仓带实参的 API 验收待完成。

## 配置

源 `.env` 有 25 个显式键，21 个与底座运行模板的键匹配。4 个网站专属键为 `NEXT_PUBLIC_API_BASE_URL`、`DEV_PORT`、`PORT`、`DANG_LOCAL_FRONTEND_PORT`。底座服务端口单独由 `DANG_BACKEND_PORT` 决定。源端实际调用 `Settings.from_env()` 可以解析数据库连接和两个供应商凭据；没有调用供应商。源端没有单设读连接，设置模型将读连接回落至主连接。本结论只描述配置解析，未验证数据库角色为只读角色。

独立仓 `E:/code/investmind-data-platform/.env` 尚不存在。已用 `git check-ignore -v` 确认它命中 `.env*`，且 `git ls-files` 没有跟踪记录。包内 `database.dump`、`manifest.json`、`verification.json` 三条目标路径全部命中 `backend/backups/`，均未被跟踪。Docker 构建上下文同时排除了这两类文件。

## 源端恢复点

源目录为 `backend/backups/20261114T124926Z-30c2e10cdc2d4fb8b0d3723194acb643`。目标计划使用独立仓同一相对目录。源端逐文件计算了完整 SHA-256。

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| database.dump | 424481381 | `0f012b943e8771d5aeb8e2d7b8e9caa94bc1acd5c9d251f3812abeb3ee4f2b23` |
| manifest.json | 32931 | `5c33ab88a7eaed3e5433d0b8f61c44eefe2b9718833ad64a9d29f8d7af74efb7` |
| verification.json | 637 | `ab19c1ba7bb330298edaeb6d54548d861938a5253cd4941901621b9d2a6c2876` |

回执的归档摘要与大小和清单一致，回执内的清单摘要也与文件一致。此前隔离恢复为 `completed / restore_verified`，66 张表，异常数 0。本次以源端文件重验完整性，未执行新恢复。

## 待交付

自动审批拒绝把包含数据库与供应商凭据的配置、含业务数据的上述归档复制到另一仓库。需就具体载荷与本机目标获得明确授权。源和目标均保持原状。获授权后可限定为该包与 21 个底座键，使用受限文件权限、集内原子交付，并保留源件；若目标已有文件，应先核实而不覆盖。

交付后要在独立仓用实际 `Settings.from_env()` 核验读连接派生值、独立仓相对路径和供应商配置；以关闭新闻后台任务的应用工厂调用健康检查及已发布数据接口，再用独立仓 `backup-status` 核验同源及 `restore_verified`。启动采集、启停既有服务和调用供应商均不属于本次交接验收。
