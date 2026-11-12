# 2B-2任务16交付记录：Requirement与Proposal Analysis Attempt白名单计划

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务16的离线契约验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。未调用真实模型、未接真实CRM、未使用真实数据库。

## 1. 实际交付范围

- `src/presales/domain/requirement_plan.py`：`RequirementOrigin`（来源版本、证据ID、定位、基线需求键）、`RequirementDraft`（模型建议文本+出处）、`Requirement`（服务端身份，`req-` 前缀强校验，带 `baseline_count`）、`PlanNode`/`AnalysisPlan`（白名单节点、依赖边、环检测、关键专家不可删）、`full_analysis_plan()` 固定顺序。
- `src/presales/application/requirement_analysis.py`：`RequirementRouter` 只在批准范围内选择计划，删专家、重复节点、意外节点的请求一律 `plan_not_allowed`；`RequirementAnalysis` 生成服务端稳定 ID（`digest` 派生）、校验每条需求出处属于已声明基线键且全部基线键被覆盖、拒绝重复文本与空基线；`merge`/`split` 保留全部原始出处，`baseline_count` 不因合并或拆分而变化；`RequirementCapacityExceeded` 在超容量时拒绝而不截断。
- `src/presales/application/opportunity_context.py`：新增 `ContextLimits`（`read_seconds`、`max_characters`、`max_records`，带范围约束与 `effective()`）；`OpportunityContextReader` 支持注入 limits，读取上限不再写死；`OpportunityContext` 支持注入 `budget`/`active`，逐次读取前调用 `register_call(...kind="mcp"...)` 把背景读取计入统一主动预算，重试次数上限仍不超过 `1 + READ_CONTEXT_RETRIES`。
- `src/presales/infrastructure/opportunity_workspace.py`：`WorkspaceAdapter` 的读取秒数改为可注入参数（默认仍为 10 秒，越界拒绝）。
- `src/presales/application/proposal_attempt_commands.py`：`AttemptStartSettings.background_limits` 新增；启动时把背景阈值实际生效值同时写入启动回执 `background_limits` 与执行记录 `background-limits`。

未实现：证据/架构/风险/汇总专家节点、Reflection、落稿、图装配、HTTP/SSE、前端页面；这些分别属于任务17—23与任务27—30。

## 2. 可判定的行为

计划约束：`full_analysis` 计划恒为 requirements→evidence→architecture→risk→summary→reflection→finalize，依赖边固定；请求删任一关键专家、给出重复节点或额外节点都返回 `plan_not_allowed`。`requirements_only` 只产生单节点计划。计划本身在构造时拒绝环、缺失依赖与白名单外节点。

需求身份：需求 ID 由服务端从 Attempt+序位+文本派生，客户端无法自带（非 `req-` 前缀直接校验失败）。相同输入重建得到相同 ID，可复现映射。

出处守恒：每条需求必须至少一个 `RequirementOrigin`，且其 `baseline_requirement_key` 必须属于本次声明的基线键集合；所有基线键必须被至少一条需求覆盖，否则 `requirement_provenance_failed`。

分母不因拆合改变：`merge` 与 `split` 保留原始出处，`baseline_count` 恒等于声明的基线键数量；单条合并、单条拆分及重复索引都被拒绝。

容量：需求文本总量超过 `max_characters` 时 `capacity_exceeded`，不截断、不静默丢弃。

预算：每次背景读取前向预算账本登记一次 `mcp` 调用；`timeout`/`contract_failed`/`capacity_exceeded` 不重试、只计一次；`unavailable` 最多重试至 `1 + READ_CONTEXT_RETRIES`，每次均记账；预算耗尽在读取前即停（`attempt_budget_exhausted`），不会发生「未记账的读取」。

阈值可核验：`ContextLimits` 缺省值与 `effective()` 一致，非法值（0、负、越界）在构造时拒绝；启动回执与执行记录中的 `limits` 与配置一致。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC07 不能删关键专家 | 反例：请求只给 requirements+evidence → `plan_not_allowed`；重复节点同拒绝 | `test_router_refuses_dropped_expert_and_duplicate_nodes` |
| AC07 不开循环 | 正例：固定顺序与依赖边可断言；反例：构造互依计划 → `ValueError`；缺失依赖与白名单外节点同样拒绝 | `test_full_plan_keeps_every_critical_expert_in_fixed_order`、`test_plan_rejects_cycles_and_unknown_nodes` |
| AC07 意图收敛 | 正例：`requirements_only` 单节点；反例：该意图下多给节点 → `plan_not_allowed` | `test_router_requirements_only_returns_a_single_node` |
| AC08 拆合保留原始出处 | 正例：合并后出处并集不变、拆分后每片保留原出处与 `derived_from` | `test_merge_keeps_origins_and_does_not_reduce_the_baseline`、`test_split_keeps_every_original_origin` |
| AC08 未经确认不改分母 | 正例：合并/拆分后 `baseline_count` 仍等于基线键数；反例：单条合并、单条拆分被拒 | `test_merge_of_a_single_item_is_refused`、`test_split_with_one_piece_is_refused` |
| AC09 出处必须真实可归属 | 反例：未知基线键 → `requirement_provenance_failed`；基线未被覆盖同样拒绝 | `test_unknown_or_missing_origin_coverage_is_refused` |
| AC09 关键输入装不下明确拒绝 | 反例：101 字符超 100 上限 → `capacity_exceeded`；正例：恰好 100 字符接受 | `test_requirement_capacity_refuses_instead_of_truncating` |
| AC19 稳定 ID 服务端产生 | 正例：重建同输入得到相同 `req-` ID；反例：客户端伪造非 `req-` ID → 校验失败 | `test_requirements_get_server_ids_and_keep_origins`、`test_requirement_id_cannot_be_invented_by_the_caller` |
| AC19 背景读取计入统一预算 | 正例：成功读取记 1 次 `mcp` 调用且携带 active；反例：超时/契约失败/容量超限不重试且只记 1 次；不可用重试至上限逐次记账 | `test_successful_read_charges_the_ledger_once`、`test_timeout_and_contract_failure_are_not_retried`、`test_unavailable_is_retried_up_to_the_cap_and_charged_each_time` |
| AC19 预算耗尽即停 | 反例：账本拒绝后读取器零调用 | `test_exhausted_budget_stops_before_calling_the_reader` |
| AC19 不以本地常量绕过账本 | 反例：尝试设置超过技术重试上限的 `max_attempts` → `ValueError`；正例：收紧上限时重试次数与记账同步减少 | `test_attempt_cap_cannot_exceed_the_technical_retry_limit`、`test_tighter_attempt_cap_stops_earlier_but_stays_charged` |
| AC19 阈值缺省与实际值可核验 | 正例：缺省值等于常量、启动回执与执行记录一致；反例：非法阈值构造失败 | `test_limits_have_verifiable_defaults`、`test_invalid_limits_are_refused`、`test_effective_background_limits_are_recorded_at_start` |
| AC19 读取秒数受限 | 正例：声明 600 秒被钳制为配置的 3 秒 | `test_declared_read_seconds_is_clamped_to_the_configured_limit` |
| AC19 未确认拒绝且零记账 | 反例：`unconfirmed` 在读取与记账前 `NotReady` | `test_unconfirmed_requirement_is_refused_before_any_charge` |
| AC19 必需失败仍闭合 | 反例：无权 → `AccessDenied`；容量超限 → `ContextCapacityExceeded`，两者都只记账一次 | `test_required_denial_and_capacity_still_fail_closed_with_budget` |

未在本步验证（留到后续步骤）：四专家产出与 Schema 校验（任务17—20）；Reflection 前后版关联（任务21）；落稿事务（任务22）；图装配与重入复用（任务23）；真实并发与崩溃时序（任务24）；语义质量与真实模型（任务26）；SSE 与运行页（任务27—30）；整体退出评审（任务33）。

### A层：离线工程验证

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest -q`（开工基线） | 492通过、111跳过 |
| `uv run --no-sync pytest tests/presales/test_requirement_analysis.py -q` | 15通过、0跳过 |
| `uv run --no-sync pytest tests/presales/test_opportunity_context_budget.py -q` | 15通过、0跳过 |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_start.py -q` | 28通过、0跳过（含新增的背景阈值生效值用例） |
| `uv run --no-sync pytest tests/presales/test_opportunity_context.py tests/presales/test_opportunity_mcp_process.py -q` | 无失败（存量用例未被可配置阈值改动破坏） |
| `uv run --no-sync pytest -q`（收工） | 523通过、111跳过、0失败（+31通过，跳过无增量） |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check <改动文件>` | 8 个改动文件均已格式化 |
| `uv run --no-sync pyrefly check` | 0错误 |

### B层：真实PostgreSQL

未执行。本步全部为离线契约验证，未新增迁移、未改动数据库约束，也无须真库才能判定的验收项；真库结论留到任务24 的并发专项。

### C层

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。

## 4. 未实现项、风险与后续任务

- 相对开工说明第 4 块的偏离：无新增或遗漏文件；`RequirementItem`（`proposal_package_sections.py`）保持不变，本步新建的 `Requirement` 是节点内身份与出处契约，不与最终 Proposal Package 区块重复权威；两者合并留给任务20 的汇总校验。
- `OpportunityContext` 的预算记账只登记「一次读取」，不区分读取耗时；真实秒级计量沿用 `active` 记录的起止区间，由任务23 的图装配统一结算。
- `register_call` 的重试上限沿用 `AttemptBudget.max_mcp_retries`，但 `OpportunityContext` 自身还有 `max_attempts` 上限；两者同时生效时取更严格的一方，已在测试中固定为「不得超过 1+2」。
- `AttemptStartSettings` 新增 `background_limits` 字段会进入启动参数摘要；同键异参比较因此包含阈值，属预期收紧，需在后续前端/HTTP 层同步提示。
- `ruff format --check` 在全仓范围仍有 8 个既有未格式化文件（`access/jwt.py`、`access/policy.py`、`application/auth.py`、`application/service.py`、`domain/contracts.py`、`infrastructure/config.py`、`infrastructure/database.py`、`maintenance/service.py`），均不在本步改动范围内，未处理。
- 语义质量不在本步范围：计划合法、出处守恒与预算闭合均不代表模型给出的需求拆解在业务上正确。
