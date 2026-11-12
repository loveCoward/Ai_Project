# 2B-2任务13交付记录：Opportunity只读背景与可信来源适配

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务13的离线契约与迁移验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实stdio往返、进程回收与短期凭证隔离由任务14承接。

## 1. 实际交付范围

- `0010_opportunity_background` 迁移：新增 `presales.opportunity_background_records`，持久化商机背景/历史记录（稳定ID、来源版本、类型、正文、来源声明、可信状态、生效时间）。经用户批准采用方案B（新增持久化表），不新建独立ACL、不接真实CRM。
- 表级约束：`source_version_id` 唯一；触发器强制来源版本所属资源必须是本商机，否则 `background_source_owner_mismatch`；`trust_state` 是唯一可更新列，内容与来源为不可变；终止态可信状态（`expired`/`conflicting`）不可回升。
- 最小权限：`presales_runtime` 仅 `SELECT,INSERT` 加 `UPDATE(trust_state)`，不授予内容改写权；`presales_maintainer` 只读。
- `src/presales/application/opportunity_context.py`：`OpportunityContextReader` 只读已登记背景并在返回前校验来源归属、`source_kind='original'`、`ready` 与声明一致；`OpportunityContext` 落实背景必需性三态与有界重试。
- `src/presales/infrastructure/opportunity_workspace.py`：只读 stdio 服务入口，注册且仅注册 `read_context`；`WorkspaceCredential` 签发 60 秒、绑定单一商机的令牌，调用方不能自带 actor/端点。
- 背景总量上限：`MAX_CONTEXT_CHARACTERS=60000`、`MAX_CONTEXT_RECORDS=200`，读取前先做 `COUNT_BACKGROUND_SQL` 计数，超限返回 `capacity_exceeded`（`context_capacity_exceeded`）而不是静默截断；`required` 遇超限抛 `ContextCapacityExceeded` 停止分析，`optional` 记录降级继续。
- 启动容量预检：`ProposalAttemptStart` 通过 `background_characters()` 把商机背景字符计入 `max_key_input_characters`，背景与关键输入合计超限即 `InvalidInput`，不创建 Attempt。

未实现：分析图、四专家链、落稿、HTTP路由、前端页面、真实CRM与任何写工具。

## 2. 七类读取结局与必需性策略

`read_context` 返回七类互斥结局：`content`（成功有内容）、`empty`（成功空历史）、`not_found_or_not_allowed`（无权与不存在对外不可区分）、`timeout`（超过10秒上限）、`unavailable`（调用或存储失败）、`contract_failed`（来源未登记或归属不符）、`capacity_exceeded`（背景总字符数或记录数超过上限，拒绝而不截断）。

必需性策略：`required` 读取失败即停止完整分析（`AccessDenied`/`ContextUnavailable`），不降级继续；`optional` 失败返回带 `degradation` 标记的结局并继续；`unconfirmed` 在任何读取前直接拒绝（`NotReady`），不在运行中翻标记。技术重试上限为初次后最多2次，超时与契约失败立即停止重试。

容量策略：单条背景正文由表约束限制在 `1..20000` 字符；单个商机聚合上限为 60000 字符 / 200 条，超出即 `capacity_exceeded` 且 `sources` 为空，不返回部分内容；`capacity_exceeded` 属于确定性失败，不进入重试。

契约变更登记：本步按 AC19「超限可见、不静默截断」把背景读取结局由计划锁定时的六类扩展为七类（新增 `capacity_exceeded`，错误码 `context_capacity_exceeded`）。该扩展已获用户确认保留，并按计划 v1.3 要求登记到任务33 二B退出评审，本记录不自行更改设计文档已批准结论。

可信状态：只有 `confirmed` 记录 `usable_as_fact=True`；`pending_confirmation`、`expired`、`conflicting` 记录仍可读取但不可作为确定事实，商机专有信息不自动升为客户通用记忆。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC09/19 来源可解析 | 正例：返回稳定记录ID、来源版本ID、声明、可信状态与生效时间 | `test_registered_background_is_returned_with_traceable_source` |
| AC09 成功空历史≠失败 | 正例：无记录返回 `empty` 且 sources 为空 | `test_success_empty_history_is_distinct_from_failure` |
| AC09 待确认不可作事实 | 正例：`pending_confirmation` 可读但 `usable_as_fact=False` | `test_pending_trust_is_returned_but_not_usable_as_fact` |
| AC04/05 无权与不存在不区分 | 反例：无授权与不存在商机返回完全相同结局 | `test_no_grant_is_indistinguishable_from_missing_opportunity` |
| AC09 未登记来源契约失败 | 反例：来源未就绪 → `contract_failed`；来源属其他商机 → `contract_failed` | `test_unregistered_source_fails_the_contract`、`test_source_pointing_at_another_opportunity_fails_the_contract` |
| AC19 超时 | 反例：越过10秒上限返回 `timeout` 且带 `context_deadline_exceeded` | `test_deadline_expiry_returns_timeout` |
| AC19 调用失败 | 反例：存储不可用返回 `unavailable` | `test_storage_failure_returns_unavailable` |
| AC19 必需失败停止/可选降级 | 正例：必需抛 `ContextUnavailable` 且重试恰好3次；可选返回降级结局 | `test_required_background_failure_stops_and_optional_degrades` |
| AC19 未确认拒绝 | 反例：`unconfirmed` 在任何读取前 `NotReady`，读取器零调用 | `test_unconfirmed_requirement_is_refused_before_any_read` |
| AC19 任务授权不上推 | 反例：无商机授权时必需背景读取 `AccessDenied` | `test_required_background_without_task_grant_is_denied` |
| AC04/05 任务授权不向上 | 反例：仅有任务授权（无商机授权）时读取返回 `not_found_or_not_allowed` 且 `sources` 为空、必需抛 `AccessDenied`；同场景可选返回 `not_found_or_not_allowed` 降级并带 `background_not_found_or_not_allowed`，不尝试读取再让模型过滤 | `test_task_grant_does_not_escalate_to_opportunity_background` |
| AC22 只信任注入凭证 | 正例：合法令牌通过；空令牌 `not_found_or_not_allowed` 且不触达读取器 | `test_tool_trusts_only_the_injected_token` |
| AC22 令牌绑定商机 | 反例：其他商机令牌被拒且零读取 | `test_token_for_another_opportunity_is_rejected` |
| AC22 不接受 actor/端点注入 | 反例：请求体携带 actor 或 endpoint 直接校验失败 | `test_request_rejects_caller_supplied_identity_and_endpoint` |
| AC19 只注册只读工具 | 静态断言：工具集合恰为 `{read_context}` | `test_server_registers_only_the_read_only_tool` |
| 凭证强度 | 负例：短密钥拒绝 | `test_settings_require_a_dedicated_key` |
| AC09/16 来源归属（真实PG） | 正例：本商机来源可插入；反例：其他商机来源被拒且未落库 | `test_background_record_requires_a_source_owned_by_that_opportunity` |
| AC19 最小权限（真实PG） | 反例：runtime 改写正文被拒，内容保持原值 | `test_runtime_cannot_rewrite_background_content` |
| AC19 背景超限拒绝不截断 | 反例：3×5000 字符超 10000 上限 → `capacity_exceeded`、`sources` 为空、`character_count=15000` | `test_oversized_background_is_refused_instead_of_truncated` |
| AC19 容量边界正例 | 正例：恰好 15000 字符仍返回 `content` 且三条全在 | `test_background_exactly_at_the_limit_is_still_readable` |
| AC19 记录数上限 | 反例：3 条超 2 条上限 → `capacity_exceeded` | `test_too_many_background_records_is_refused` |
| AC19 容量参数校验 | 反例：`max_characters=0` 抛 `ValueError` | `test_capacity_bounds_must_be_positive` |
| AC19 必需超限停止/可选降级 | 反例：必需抛 `ContextCapacityExceeded` 且零重试；可选返回 `capacity_exceeded` 降级 | `test_required_capacity_exceeded_stops_instead_of_degrading` |
| AC19 启动容量计入背景 | 反例：100 字符关键输入 + 200 字符背景超 150 上限 → `InvalidInput` 且未创建 Attempt | `test_background_counts_towards_key_input_capacity` |
| AC19 启动容量边界 | 正例：100 + 100 = 200 恰好等于上限 → `accepted` | `test_background_within_capacity_boundary_is_accepted` |

未在本步验证（留到后续步骤）：真实stdio往返、子进程启动/回收、短期凭证在真实传输中的隔离、EOF与超时行为（任务14）；分析图与四专家链（任务16—19）；SSE与运行页（任务27—30）。

### A层：离线工程验证

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_opportunity_context.py -q` | 22通过、2跳过（postgres用例按既有opt-in跳过；含新增的「任务授权不向上」用例） |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_start.py -q` | 27通过、0跳过、0失败（含2条背景容量用例） |
| `uv run --no-sync pytest tests/presales -q` | 283通过、94跳过、0失败（跳过数相对开工基线无增量） |
| `uv run --no-sync pytest -q`（仓库全量离线） | 475通过、98跳过、0失败（开工基线474/98，新增1条，跳过无增量） |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check <改动文件>` | 通过，7个文件均已格式化 |
| `uv run --no-sync pyrefly check` | 通过，0错误 |

### B层：真实PostgreSQL（前次已执行；本次补测未执行）

下表命令为2026-11-12 前次会话的执行结果，属历史工程证据。本次补齐用例后未获B层再授权，故 `opportunity_context` 专项与全量档均未重跑，任务13真库结论不代表本次复验通过。

环境：本机 PostgreSQL 18.6 临时回环集群、PGVector 0.8.6、专用测试库；全合成数据；未连接共享或客户库。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k migration_upgrade_head_twice` | 1通过，迁移可重复升级至 `0010_opportunity_background` |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k opportunity_context` | 23通过、0失败 |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --full-suite --pytest-args -q -rs` | 567通过、5跳过、0失败（182.97秒；基线560/5，新增7条，跳过数无增量） |

本次新增离线用例后，上述两条命令的预期收数为 `24` 与 `568`；因未获本次B层授权，未执行、未核实。

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。

## 4. 未实现项、风险与后续任务

- 本记录不包含stdio真实往返与进程生命周期结论；该验证已在任务14执行，结果见 `docs/maintenance/phase2b-2-task14-delivery-2026-11-12.md`，本记录的离线契约边界不外推。
- 背景单条正文上限20000字符由表约束保证；单商机聚合上限为60000字符/200条，超限即拒绝而非截断。当前规模下不实现分页：`read_context` 一次最多读取200条，超限直接失败并提示缩小背景范围；该规模假设与「不引入分页」的决定已按计划v1.3登记到任务33退出评审，若后续业务要求更大单商机背景，需届时重新评估分页与预算方案。
- `read_context` 的10秒上限目前仍是 `opportunity_context.py` / `opportunity_workspace.py` 的模块常量，尚未与 `ProposalAttemptContext` 的调用预算账本联动。计划v1.3已把「背景读取接入 `AttemptBudget`（`max_mcp_seconds`/`max_mcp_retries`）、阈值配置化并记录实际生效值」补入任务16，含 `tests/presales/test_opportunity_context_budget.py` 正反例；本步按任务13范围未实现该接入。
- `OpportunityContext` 的技术重试实际是「初次 + 2 次」固定常量 `READ_CONTEXT_RETRIES`，未区分可用预算余额；重试次数不随 `AttemptBudget` 变化，属任务16范围。
- 本步新增的「任务授权不向上」用例仅为离线 SQLite 替身断言，未在真实角色/授权存储上验证任务授权与商机授权的差别；真库等权范围由任务14的授权专项报告承接。
- 商机背景表未定义归档或清理策略，与执行记录一致按「只增不删」处理，容量风险在二B退出评审登记。
- 本次顺带更新 `test_proposal_storage_postgres.py` 的 head 断言（`0009`→`0010`），属过时断言同步，非放宽。
- 本次未改 `STATUS.md` 的阶段验收结论。
