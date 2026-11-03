# 阶段一：本地开发与维护说明

- 状态：阶段一工程实现及专项验证完成后的操作说明；不得据本文宣称产品验收或生产可用。
- 范围：[阶段一设计方案](../superpowers/specs/阶段一-基础设施与领域数据设计方案.md)。
- 需求：[已批准业务基线](../superpowers/specs/初版业务需求分析文档.md)。

## 运行边界

阶段一只有 Python 应用服务、数据库访问层和显式维护命令；没有售前 HTTP 路由、登录 UI、Agent、知识检索、外部 MCP 写入或部署操作。原有通用聊天入口仍按脚手架认证规则运行，不能用于售前对象和 thread。

仅使用本地 PostgreSQL 及合成演示资料。禁止连接现有业务库、提交 `.env`/私钥或将真实客户敏感材料放入文件目录。命令从明确的环境变量读取连接；Pydantic Settings 不自动读取仓库 `.env`。所有 URL 使用 `postgresql+psycopg://`，账号口令只存于本机秘密管理工具或进程环境，不写进命令行、PowerShell 历史或文档。

## 配置

| 变量 | 用途 | 账号边界 |
| --- | --- | --- |
| `PRESALES_DATABASE_URL` | 应用运行时池 | `presales_runtime` 成员；不得拥有 DDL、IAM 写入或维护能力 |
| `PRESALES_MIGRATION_URL` | 显式 Alembic 迁移 | 受控迁移身份；首次安装还需创建两个 `NOLOGIN` 权限角色，因此须由 DBA 预建角色或授予迁移账号相应能力 |
| `PRESALES_BOOTSTRAP_URL` | 只运行一次的演示种子 | 单独受控 bootstrap 身份；成功后不提供给应用进程或长期维护进程 |
| `PRESALES_MAINTENANCE_URL` | 本机授权预览/应用及文件 reconciliation | `presales_maintainer` 成员；不得写商机、任务或版本正文 |
| `PRESALES_OPERATOR_ALLOWLIST` | 允许运行维护命令的 OS 身份列表 | `uv run python -m presales.maintenance operator-id` 输出本机可核验身份；多值逗号分隔 |
| `PRESALES_FILE_ROOT` | 文件受控根目录 | 专用本机目录；不得设为仓库公开静态目录或共享下载目录 |
| `PRESALES_POOL_SIZE` | 数据库池容量 | 默认3，范围1-20 |
| `PRESALES_LOCK_TIMEOUT_MS` | 短事务锁等待上限 | 默认5000毫秒 |
| `PRESALES_STATEMENT_TIMEOUT_MS` | SQL语句时限 | 默认15000毫秒 |

连接 URL 含 `@`、`:`、`/` 等字符的口令须按 URL 规则编码。不要把完整连接字符串打印到诊断或工单中。`presales_runtime`、`presales_maintainer` 是数据库组角色，不应设置为可登录角色；管理员须先建立可登录身份并只授予对应组角色，再分别配置运行与维护环境。

## 空数据库安装

1. 确认目标是专用、可丢弃的数据库且版本为 PostgreSQL 18；若有可复现兼容阻断，按阶段一方案统一回退 PostgreSQL 16。本说明不启动、安装系统服务，也不改写现有数据库。
2. 由受控 DBA 预建 `presales_runtime`、`presales_maintainer` 组角色，或使用受控迁移身份创建；迁移版本由 Alembic 管理，不由 Web 服务自动升级。
3. 以 `PRESALES_MIGRATION_URL` 运行 `uv run alembic upgrade head`，确认 Alembic revision。迁移连接须有创建 schema/对象及管理权限组角色所需的权限；普通运行身份不得迁移。
4. 配置 OS allowlist 和受控 `PRESALES_BOOTSTRAP_URL`，执行 `uv run python -m presales.maintenance initialize`。命令只写三个合成演示主体和一个合成 Opportunity；重跑只报告已初始化，不重置授权。
5. 收回 bootstrap 凭据；分别创建/验证 `presales_runtime` 与 `presales_maintainer` 登录身份，不能让运行进程持有迁移或 bootstrap 凭据。

维护命令的操作者身份从 OS 读取，不支持 `--actor` 冒名。准备本地 JSON 变更集后预览：`uv run python -m presales.maintenance authorization preview --file <本地变更文件>`。确认输出的目标和前后值后，显式执行 `authorization apply --id <变更ID> --digest <预览摘要> --reason <原因>`。扩权另须 `--confirm-expansion`；响应丢失用 `authorization status --id <变更ID>` 核对。原因中不得包含密钥、JWT 或客户正文。

文件 reconciliation 只接受已有 readiness ID：`uv run python -m presales.maintenance readiness reconcile --id <readiness-id>`。失败保持不可见；不自动删除孤儿文件，不凭文件存在开放读取。

## 验证

不需要模型凭据的单元测试：`uv run pytest tests/presales/test_units.py`。

阶段一 PostgreSQL 验收：`uv run python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --pytest-args -q`。脚本在随机临时目录创建独立回环实例、专用数据库和短期随机测试凭据；凭据不会打印。测试库必须有固定名称和只由脚本建立的 `phase_one_test_marker`；测试夹具会拒绝其他目标。Windows 下 psycopg asyncio 使用 `SelectorEventLoop`，符合 [Psycopg Windows asyncio 要求](https://www.psycopg.org/psycopg3/docs/advanced/async.html)。脚本退出时停止实例并仅清理它创建的临时目录。

该命令不覆盖已有服务、实例或数据目录。缺少 PostgreSQL 二进制/临时权限时应报告“未执行”，不能改用 SQLite 或 Fake 关闭 PostgreSQL 阶段门。
