# 2B-1任务11交付记录：Proposal Analysis Attempt重启核实与受控继续

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务11的离线工程实现、A层与B层验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实多进程竞争、进程崩溃时序与跨进程租约接管仍由任务12/24承接。

## 1. 实际交付范围

- `ProposalAttemptRecovery.inspect()` 扫描单个未终态Attempt：核对原发起人、任务授权、两级槽位归属、在途调用、结果就绪与已记录的恢复意图，输出 `terminal` / `business_committed` / `unknown_calls_pending` / `execution_unconfirmed` / `awaiting_verification` / `resume_recorded` 分类，只读不写执行记录，不重启模型调用。
- `ProposalAttemptRecovery.request_recovery()` 以稳定 `request_id` 记录恢复意图：先复核原actor（`attempt.actor`，换人即 `not_found_or_not_allowed`）、架构师角色与任务授权，再按 `expected_execution_generation`/`expected_run_revision`/`expected_task_revision`/`expected_input_revision` 逐项比对，核对checkpoint回执、来源闭包仍published、主动预算剩余与在途调用；通过后写入 `recovery-intent-<request>` 执行记录与命令回执，返回 `next_execution_generation` 与剩余预算。同键同参复用回执，异参 `revision_or_request_conflict`。
- `ProposalAttemptRecovery.close_unrecoverable()` 在确认执行停止且无 `registered`/`unknown` 调用时，把不可恢复的 `reconciling` Attempt 收束为 `finished` 并释放两级槽位；未确认停止或有未结调用时保持 `reconciling` 并 `recovery_blocked(execution_unconfirmed)`。只接受 `failed`/`materials_required` 结局，拒绝以成功收尾。
- `0009_proposal_attempt_recovery` 前向修复：重定义 `presales.guard_proposal_attempt()`，只新增一条白名单 `reconciling → running`，且要求代次恰好 +1 并存在同代次 `recovery-intent-%` 已完成记录；新增 `presales.resume_proposal_attempt(...)` `SECURITY DEFINER`（固定 `search_path`、共享授权锁、原actor与任务授权重验、终态/阶段/代次/修订/输入/恢复意图/在途调用逐项校验），按期望旧代次条件把两级槽位推进到新代次，再把 Attempt 置回 `running`。`GRANT` 仍只到函数执行权。
- 领域状态机 `_TRANSITIONS[RECONCILING]` 增加 `RUNNING`，并由 `transition_attempt(..., resumed_generation=)` 强制「必须是当前代次+1」，否则 `revision_or_request_conflict`。
- 监督器 `_claim()` 新增 `reconciling` 分支：无已记录恢复意图仍返回 `not_ready`（旧语义保留），有意图则经 `resume_proposal_attempt` 续跑并以新代次返回 `AttemptClaim`。

沿用任务7至10既有能力：Attempt身份与代次、主动预算账本、`ProposalExecutionRecords` 不变量、两级槽位唯一约束、取消与条件排空、checkpoint fencing、监督器领取/心跳/关闭。

本次没有新增HTTP/SSE入口、分析图或节点执行，没有真实模型调用。

## 2. 流程与边界

重启后的处理顺序：启动扫描只做核实与分类，绝不自动派发；架构师重新登录后调用 `inspect()` 看到分类与阻断原因；确认可恢复则 `request_recovery()` 落一条稳定意图；监督器领取时在**同一事务内**由数据库函数完成槽位代次推进与阶段回切，业务层不做跨事务「先读后代」。业务已落稿时 `inspect()` 返回 `business_committed`、`request_recovery()` 直接 `recovery_blocked(business_already_committed)`，不重新生成候选。调用结果未知时保持 `unknown_calls_pending`，不盲目重试。取消或终态的Attempt在修复、请求、领取三个入口分别断言 `attempt_terminal`。全局槽位被他人占用时 `resume_proposal_attempt` 以 `fence_conflict` 拒绝，不做排队。

安全边界：`reconciling → running` 不是无条件放开。数据库触发器仍拒绝「无恢复意图」与「代次未+1」两种越界写入（B层已断言 `invalid_proposal_attempt_transition`）；应用层 `transition_attempt` 同样拒绝。换人接管在原actor校验处即被拒，权限恢复也不复活授权失效Attempt。

离线测试使用SQLite适配（剥离 `FOR UPDATE`/`FOR SHARE`、转换 `jsonb`/`now()`、以 `_resume()` 显式模拟数据库函数语义）与Fake时钟；它只证明分类、权限、幂等、拒绝与代次推进契约。触发器白名单、函数 `SECURITY DEFINER` 边界与真实行锁顺序由B层真实实例取证，但仍不证明跨进程竞争与崩溃时序（任务12）。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC19 扫描核实不外发 | 正例：`reconciling` 分类 `awaiting_verification`、不写记录、状态不变；`resume_recorded` 分类不启动执行 | `test_inspect_classifies_reconciling_attempt_as_awaiting_verification`、`test_inspect_reports_recorded_intent_without_starting_execution` |
| AC19 终态/落稿/在途分类 | 正例：`finished` 归 `terminal`；`result_ready` 归 `business_committed`；`registered` 调用归 `unknown_calls_pending` 且调用数不增 | `test_inspect_reports_terminal_and_business_committed_and_open_calls`、`test_inspect_reports_business_committed_without_reopening`、`test_inspect_reports_open_calls_and_never_retries` |
| AC19/13 恢复意图稳定身份 | 正例：同 request 重放返回同一结果、仅一条意图、代次与阶段不变；反例：同键异参 `revision_or_request_conflict` 且不新增意图 | `test_recovery_records_stable_intent_and_replays_the_same_result`、`test_recovery_rejects_same_request_id_with_different_parameters` |
| AC19 原发起人重验 | 反例：非原actor在 `inspect`/`request_recovery` 均 `not_found_or_not_allowed`，无意图写入、代次不变 | `test_foreign_actor_cannot_inspect_or_recover` |
| AC19 终态不可复活 | 反例：已取消+`finished` 请求恢复断言 `attempt_terminal` 且无意图写入 | `test_terminal_or_cancelled_attempt_cannot_be_recovered` |
| AC19 阶段与代次/修订重验 | 反例：`running` 断言 `not_ready`；代次不符 `fence_conflict`；运行修订不符 `revision_or_request_conflict`，均不写意图 | `test_recovery_requires_reconciling_stage_and_rejects_stale_identity` |
| AC19 输入/任务修订重验 | 反例：输入修订不符 `recovery_blocked(input_changed)`；任务修订不符 `revision_or_request_conflict` | `test_recovery_rejects_changed_input_or_task_revision` |
| AC19 checkpoint与预算 | 反例：`checkpoint_missing`、`budget_exhausted` 均拒绝且状态不变 | `test_recovery_rejects_missing_checkpoint_and_exhausted_budget` |
| AC19 未知调用与已落稿 | 反例：`unknown_calls_pending`、`business_already_committed`，调用记录不增 | `test_recovery_rejects_open_call_and_business_committed` |
| AC06/19 来源失效 | 反例：来源knowledge版本非published断言 `source_invalid` 且无意图写入 | `test_recovery_rejects_source_that_is_no_longer_published` |
| AC19 不可恢复收尾 | 正例：确认停止且无在途调用后转 `finished/failed` 并释放两级槽位；反例：`execution_stopped=False` 或在途调用时 `execution_unconfirmed` 且保持占位 | `test_close_unrecoverable_finishes_attempt_and_releases_slots`、`test_close_unrecoverable_waits_for_stop_and_keeps_slots` |
| AC19 收尾拒绝终态/非待核实/成功 | 反例：`attempt_terminal`、`not_ready`、`ValueError`（禁成功收尾），状态与槽位不变 | `test_close_unrecoverable_refuses_terminal_and_non_reconciling`、`test_close_unrecoverable_rejects_success_outcome` |
| AC19 受控续跑 | 正例：有意图时领取成功，代次1→2、阶段回 `running`、运行修订+1、两级槽位代次均为2 | `test_recorded_intent_lets_the_supervisor_resume_with_the_next_generation` |
| AC19 无意图不续跑 | 反例：`reconciling` 无意图领取 `not_ready` 且代次不变；有意图续跑后再领 `attempt_busy` | `test_reconciling_attempt_without_recorded_intent_is_not_claimed`、`test_second_resume_without_a_new_intent_is_refused` |
| AC19 取消阻止续跑 | 反例：已取消的 `reconciling` 领取 `attempt_terminal`，阶段与代次不变 | `test_cancelled_reconciling_attempt_cannot_be_resumed` |
| AC19 槽位代次隔离 | 反例：全局槽位属其他Attempt时续跑 `fence_conflict`，Attempt保持 `reconciling/1` | `test_resume_is_fenced_when_a_slot_is_not_held_by_this_generation` |
| AC19 领域门禁 | 正例：代次+1允许；反例：代次+3 `revision_or_request_conflict`、缺代次 `revision_or_request_conflict`、终态 `attempt_terminal` | `test_domain_resume_requires_the_next_generation_exactly` |
| AC19 迁移门禁（真实PG） | 迁移连续upgrade两次到 head `0009_proposal_attempt_recovery`；无意图的 `reconciling→running` 与同代次的 `reconciling→running` 均断言 `invalid_proposal_attempt_transition` | `test_migration_upgrade_head_twice`、`test_trigger_rejects_reconciling_to_running_without_intent`、`test_trigger_rejects_reconciling_to_running_on_same_generation` |
| AC19 续跑函数（真实PG） | 正例：有意图时函数返回2、Attempt为 `running/2`、两级槽位代次2；反例：无意图 `recovery_blocked` 且保持 `reconciling/1` | `test_resume_function_bumps_generation_and_reacquires_slots`、`test_resume_function_requires_a_recorded_intent` |

未在本步验证（留到后续步骤）：真实多进程/崩溃点下双续跑、提交到派发间崩溃、旧owner回归拒写、真实连接中断（任务12/24）；REST/SSE与运行页的恢复入口（任务27—30）；审批 interrupt/resume 与阶段三人机等待闭环（阶段三）；分析图与节点级预算检查（2B-2）。

### A层：离线工程验证

开工基线（本次任务开始前，任务10交付后）：`tests/presales` 234通过/78跳过；全量426通过/82跳过。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_recovery.py -q` | 23通过、0失败、0跳过 |
| `uv run --no-sync pytest tests/presales -q` | 257通过、82跳过、0失败 |
| `uv run --no-sync pytest -q` | 449通过、86跳过、0失败 |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check`（本任务8个改动文件） | 通过，8个文件已格式化 |
| `uv run --no-sync pyrefly check` | 通过，0错误（11项既有抑制） |

新增用例与基线差额吻合：presales 234→257 为 +23；全量 426→449 为 +23。skipped 82→86 的 +4 全部来自新增的两个 `pytest.mark.postgres` 用例族需真实实例，属既有 opt-in 跳过规则，未新增条件性 skip、未新增 xfail。

### B层：真实PostgreSQL（已执行）

环境：本机 PostgreSQL 18.6 临时回环集群（脚本自建、随机端口、专用 `biddesk_phase1_test` 库）、PGVector 0.8.6；全合成数据；未连接共享或客户库，未使用容器。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k "proposal_storage_postgres"` | 17通过、0失败、1跳过族（deselect 322）；PGVector 0.8.6 原生距离校验通过 |

B层结论：迁移可重复upgrade至 `0009`；触发器对「无意图」和「同代次」的 `reconciling→running` 均拒绝；续跑函数只在有意图且代次条件满足时推进槽位与代次。未验证跨进程并发双续跑、崩溃时序与真实连接中断（任务12）。

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。Fake runner只证明监督器调度与隔离契约，不构成分析图或真实模型能力证据。

## 4. 未实现项、风险与后续任务

- 未接入REST/SSE与运行页的恢复入口（任务27—30）；当前恢复能力只有应用服务与监督器接口。
- 未实现分析图与节点级预算检查（2B-2）；续跑后Attempt进入 `running`，仍需真实执行者。
- 未验证真实多进程双续跑、崩溃点时序、旧owner回归拒写与连接中断（任务12/24）。
- `resume_proposal_attempt` 固定使用30秒租约，与 `SupervisorSettings.lease_seconds` 的默认值一致但未参数化；若后续调整监督器租约需同步该函数。
- 恢复意图记录永久保留为 `recovery-intent-*`，续跑后不清理；后续如需审计压缩应在维护命令中处理，不在本步删除审计痕迹。
- 本次顺带更正 `test_migration_upgrade_head_twice` 中硬编码的 head 版本（`0006`→`0009`）；这是过时断言更正，非放宽断言：该用例此前因 opt-in 跳过而未暴露。
- 本次未改 `STATUS.md` 的阶段验收结论，只按实际代码更新任务11的事实边界。
- 开工前已存在的未提交改动不属于本任务：`AGENTS.md`、`README.md`、`STATUS.md`、四份设计文档、`api/app.py`、`api/auth.py`、`infrastructure/outbound.py`、`infrastructure/proposal_attempt_store.py`（任务7至10部分）、`infrastructure/proposal_checkpoint.py`、`domain/proposal_analysis_attempt.py`（任务10部分）及任务7至10新增文件与交付记录，均未回滚。
