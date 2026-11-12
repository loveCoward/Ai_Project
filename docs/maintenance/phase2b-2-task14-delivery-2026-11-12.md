# 2B-2任务14交付记录：Opportunity只读MCP进程与授权真实集成

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务14的真实 stdio 子进程与本机临时 PostgreSQL 专项验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实模型、真实CRM、外部写入均未使用。

## 1. 实际交付范围

- `src/presales/infrastructure/opportunity_workspace.py`：补齐任务13留待任务14的运行时缺口——`main()` 增加 lifespan，退出时关闭数据库连接；Windows 入口改用 `asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)`（psycopg 异步连接在 Windows 上的必要条件）；新增 `WorkspaceClient` 后台可信调用封装，按白名单裁剪子进程环境并注入 `PYTHONPATH`，只在 `_meta.presales_context` 传短期令牌；凭证支持可选 `attempt_id` 绑定并在服务端双向校验（令牌带 attempt 则请求必须同 attempt，令牌不带 attempt 则请求不得带）；客户端自身时钟判定超时后返回 `timeout/context_deadline_exceeded`，而非把子进程异常一律记为 `unavailable`。
- `tests/presales/test_opportunity_mcp_process.py`：新增真实子进程专项。用原生 JSON-RPC 直接驱动 `python -m presales.infrastructure.opportunity_workspace`，因此可以观测 `_meta`、原始错误文本、stdin EOF 与退出码，而不经过 FastMCP 的客户端封装。
- `tests/presales/test_opportunity_context.py`：新增 attempt 绑定的离线正反例（正例：绑定一致通过；反例：请求丢掉 attempt、换 attempt、换商机均拒绝且读取器零调用）。

未实现：分析图、四专家链、落稿、SSE、前端页面、真实CRM与任何写工具；这些不在任务14范围内。

## 2. 可判定的行为

进程级真实往返：客户端 `WorkspaceClient` 与手工 JSON-RPC 子进程都返回与直接调用 `OpportunityContextReader` 相同的记录 ID 与正文，说明 stdio 链路没有改变业务结果。

授权边界：无 `_meta` 令牌、伪造令牌、非活跃主体、越权商机、撤销授权后重放，都返回 `not_found_or_not_allowed`，且不泄漏对象是否存在；请求体携带 `actor`/`endpoint` 被 DTO 的 `extra="forbid"` 直接判为工具错误；attempt 令牌不能用于别的 attempt 或去掉 attempt 后重放。

只读边界：工具集合恰为 `{read_context}` 且带 `readOnlyHint=True`；直接调用 `write_context`、`delete_context`、`publish_knowledge` 均返回 `isError`，文本为 `Unknown tool`。

失败可判定：真实不可达数据库 → `unavailable`；客户端期限耗尽 → `timeout/context_deadline_exceeded`；来源版本未就绪 → `contract_failed/context_contract_failed` 且 `sources` 为空；4×20000 字符超过 60000 上限 → `capacity_exceeded/context_capacity_exceeded`，`sources` 为空，`character_count=80000`，不返回部分内容。

进程寿命：正常关闭 stdin 后子进程退出码为 0；强制终止后 `poll()` 非空，无遗留句柄；子进程 stderr 中不含背景正文。

等权范围说明：本任务验证的是任务13交付的**商机背景只读 stdio 入口**，不是二A的 Product Catalog MCP 运行时；两者共用“后端签发短期令牌 + 服务端重建授权”的模式，但资源类型、工具与凭证 audience 不同。任务14不声称覆盖二A检索等权结论。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC04/05 伪造主体拒绝 | 反例：请求体带 `actor` → 工具错误；伪造/过期/无令牌 → `not_found_or_not_allowed` | `test_stdio_roundtrip_matches_direct_read_and_binds_attempt`、`test_stdio_seven_outcomes_of_authorization_and_trust` |
| AC04/05 撤权重放 | 反例：`maintenance` 撤销架构师对商机的授权后，同一子进程重放 → `not_found_or_not_allowed` | `test_stdio_seven_outcomes_of_authorization_and_trust` |
| AC04/05 Attempt 绑定 | 正例：同 attempt 通过且结果与直读一致；反例：去 attempt/换 attempt/换商机均拒绝，离线另断言读取器零调用 | `test_stdio_roundtrip_matches_direct_read_and_binds_attempt`、`test_attempt_binding_is_enforced_both_ways` |
| AC04/05 非活跃主体 | 反例：主体 `active=false` → `not_found_or_not_allowed` | `test_stdio_seven_outcomes_of_authorization_and_trust` |
| AC09 成功空历史≠失败 | 正例：新建商机无背景 → `empty`、`sources=[]`、`error_code=None` | `test_stdio_empty_history_is_success_not_a_failure` |
| AC09 待确认不可作事实 | 正例：`pending_confirmation` 可读但 `usable_as_fact=false` | `test_stdio_seven_outcomes_of_authorization_and_trust` |
| AC09 来源契约失败 | 反例：来源版本未就绪 → `contract_failed/context_contract_failed`、`sources=[]` | `test_stdio_contract_failure_is_reported_as_contract_failed` |
| AC19 超时可判定 | 反例：客户端期限 1ms → `timeout/context_deadline_exceeded` | `test_client_deadline_expiry_is_reported_as_timeout` |
| AC19 存储不可用 | 反例：回环端口 1 无实例 → `unavailable` | `test_child_reports_unavailable_when_database_is_unreachable` |
| AC19 容量超限不截断 | 反例：4×20000 字符 → `capacity_exceeded`、`sources=[]`、`character_count=80000` | `test_stdio_capacity_exceeded_refuses_without_returning_partial_content` |
| AC19 EOF/异常可判定 | 正例：正常关闭 stdin → 退出码 0、`poll()` 已回收；反例：`kill()` → 非零退出、`poll()` 非空 | `test_eof_and_exit_are_deterministic_and_process_is_reaped` |
| AC19 必需失败/可选降级 | 反例：必需 → `ContextUnavailable` 且恰好 3 次读取；可选 → 降级标记继续；无授权必需 → `AccessDenied` | `test_required_policy_fails_closed_and_optional_degrades` |
| AC22 直接写调用拒绝 | 反例：`write_context`/`delete_context`/`publish_knowledge` 均 `isError`、`Unknown tool` | `test_stdio_refuses_direct_write_tool_calls` |
| AC22 秘密不入响应/日志 | 正例：`_meta` 令牌不出现在工具响应；背景正文不出现在子进程 stderr | `test_stdio_roundtrip_matches_direct_read_and_binds_attempt` |
| AC22 环境最小化 | 反例：`OPENAI_API_KEY`/`PRESALES_JWT_SECRET` 不被子进程继承 | `test_client_environment_is_minimal_and_deadline_is_bounded` |
| AC09 Mock 背景落同一登记库 | 正例：子进程读取的记录来自 `presales.opportunity_background_records` 与 `resource_versions`，未使用独立 ACL 或未登记字符串 | 全部真实 stdio 用例共用 `source()`/`background()` 登记路径 |

未在本步验证（留到后续步骤）：四专家链与分析图（任务16—19）；Evidence Snapshot 来源闭包（任务15）；SSE 与运行页（任务27—30）；真实模型能力（任务26）；整体退出评审（任务33）。超时的“业务级 10 秒”只验证了客户端期限参数与子进程内 10 秒上限存在，未构造真实的 10 秒慢查询。

### A层：离线工程验证

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest -q`（开工基线） | 475通过、98跳过、0失败 |
| `uv run --no-sync pytest tests/presales/test_opportunity_mcp_process.py -q` | 4通过、8跳过（8条`@postgres`用例按既有opt-in跳过，共12条） |
| `uv run --no-sync pytest tests/presales/test_opportunity_context.py -q` | 23通过、2跳过 |
| `uv run --no-sync pytest -q`（收工） | 480通过、106跳过、0失败；新增5条离线用例，跳过增量8全部是新增postgres用例的既有opt-in规则 |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check <改动文件>` | 通过 |
| `uv run --no-sync pyrefly check` | 0错误 |

### B层：真实PostgreSQL与真实stdio子进程（本次已执行）

环境：本机 PostgreSQL 18.6 临时回环集群、PGVector 0.8.6、脚本自建自删；全合成数据；未连接共享库或客户数据；未使用容器。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k opportunity_mcp_process` | 12通过、0失败（真实子进程往返、授权、写拒绝、容量、EOF/终止） |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --full-suite --pytest-args -q -rs` | 581通过、5跳过、0失败（160.76秒；跳过均为既有 docker/symlink 规则，与开工基线的5条相同） |

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。

## 4. 未实现项、风险与后续任务

- 真实 10 秒慢查询未构造。当前 10 秒上限是 `opportunity_context.py` 与 `opportunity_workspace.py` 的模块常量；接入 `AttemptBudget`、阈值配置化与真实慢查询边界见任务16。
- 子进程“无残留”断言只覆盖本测试启动的进程；未做整机进程表扫描，也未覆盖 Job Object 在父进程被强制终止时的回收。后者属于 Windows 部署级验证，未在任务14声明。
- 任务13交付记录中“stdio 未验证”的表述已在本记录指向任务14；任务13自身测试边界不外推。
- 背景表无归档/清理策略，容量与只增不删的风险在二B退出评审登记（任务33）。
- 本任务的 stdio 入口只服务商机背景，不等同二A Product Catalog MCP 运行时；不得以本报告替代二A检索等权结论。
