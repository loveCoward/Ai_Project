# 2B-1任务9交付记录：Proposal Analysis Attempt监督器与售前入口寿命

日期：2026-11-11（Asia/Shanghai）。本记录只覆盖任务9的离线工程实现和A层验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实多进程竞争、进程崩溃与跨进程租约接管仍由任务12承接。

## 1. 实际交付范围

本次新增售前分析监督器并装配到售前ASGI生命周期：

- `ProposalAttemptSupervisor` 以数据库条件在短事务内领取执行代次：只接受 `accepted` 阶段，同一事务内读取Attempt（`FOR UPDATE`）、核对任务槽位与全局槽位同属本Attempt及当前代次、租约未过期，再条件更新为 `running` 并递增运行修订。领取成功后才允许派发。
- `heartbeat()` 只对 `running` 阶段刷新两级槽位的 `lease_until`；终态、已取消返回 `attempt_terminal`，非运行阶段返回 `not_ready`，槽位不属于本代次返回 `fence_conflict`，不扩大任何权限。
- `scan()` 只核对不外发：对未终态Attempt输出 `accepted_not_dispatched`/`needs_reconciliation`/`awaiting_reconciliation`/`slot_missing` 分类并写入已完成执行记录；租约过期且仍在 `running`/`waiting_human` 的只标记 `reconciling` + `restart_unconfirmed`，绝不抢占、不派发。
- 关闭流程先停止领取（`notify`/`submit` 返回 `supervisor_stopped`），在 `shutdown_seconds` 内等待本地执行；超限取消本地调用并把仍无法确认的Attempt标记 `reconciling` + `shutdown_unconfirmed`，保留两级槽位，不写终态、不释放占位。
- 运行期数据库不可用（`StorageUnavailable`）时监督器进入 halted，停止新外发并拒绝后续领取、心跳与扫描；不能仅凭内存授权继续执行。
- 图执行只经注入的 `AttemptRunner` 端口；未注入时 `dispatch` 抛出 `analysis_runtime_unavailable` 且不改变Attempt状态，运行模块不导入通用 `agents`/`service.service`。
- `create_auth_app` 增加可选 `background` 生命周期参数，与兼容模式saver用 `AsyncExitStack` 组合；`create_app` 在售前应用装配 `supervisor_lifecycle` 并暴露 `app.state.supervisor`，默认不注入runner。

沿用2B-1任务7/8既有能力：Attempt身份与代次、`ProposalExecutionRecords` 不变量（代次校验、终态与取消拒绝写入）、两级槽位唯一约束、`RunProjection` 阶段枚举、任务 `analyzing` 状态。

本次没有新增迁移，没有实现分析图/节点/四专家链、取消命令与排空、业务恢复消费、REST/SSE 入口或运行页。

## 2. 流程与边界

启动命令提交后Attempt处于 `accepted`。监督器被唤醒后先领取：代次与槽位不匹配即 `fence_conflict`，同任务已有运行中Attempt即 `attempt_busy`，非 `accepted` 阶段（`reconciling`/`waiting_human`）即 `not_ready`，已取消或 `finished` 即 `attempt_terminal`；以上均不改状态。领取成功写入 `running` 并对齐运行修订，随后通过注入端口执行。请求返回、关闭页面或退出登录都不取消监督器任务，因为执行与请求生命周期解耦。

重启后的首次启动只做核对：`accepted` 记录标注“受理未派发”，租约已过期的 `running` 记录转 `reconciling` 待人工核实，绝不自动重发模型调用；`reconciling` 记录保持原状。心跳超时因此只作为核实信号，不构成接管依据。关闭期限超限时同样保留 `reconciling` 与占位，宁可保守占用也不提前释放给旧执行。

离线测试使用SQLite适配（显式剥离 `FOR UPDATE`/`FOR SHARE`、转换 `jsonb` 与 `now()`）、Fake时钟与注入端口。它只证明领取/心跳/扫描/关闭/拒绝的分类与状态迁移契约，不证明PostgreSQL行锁顺序、跨进程双领取、真实连接中断或进程崩溃时序；这些证据须由任务12在真实临时实例取得。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC19 领取单一执行者 | 正例：`accepted→running`、代次与thread保持、运行修订+1、两级槽位保留；反例：二次领取 `attempt_busy` 且不重复派发 | `test_claim_moves_accepted_attempt_to_running_and_keeps_slots`、`test_second_claim_of_same_attempt_is_busy_and_does_not_redispatch` |
| AC19 非accepted阶段拒绝 | `reconciling`/`waiting_human` 断言 `not_ready`、`running` 断言 `attempt_busy`，状态均未迁移 | `test_claim_outside_accepted_is_refused_without_transition` |
| AC19 终态与取消不可复活 | `finished` 与带 `cancelled_at` 的Attempt在领取、派发、心跳三个入口分别断言 `attempt_terminal`，端口零调用 | `test_terminal_attempt_cannot_be_claimed_or_dispatched`、`test_cancelled_attempt_is_terminal_for_claim_and_heartbeat`、`test_heartbeat_after_cancellation_is_terminal` |
| AC19 代次隔离 | 槽位缺失、全局槽位属于其他Attempt均断言 `fence_conflict` 且不迁移 | `test_missing_task_slot_fences_heartbeat`、`test_claim_with_foreign_global_slot_is_fenced` |
| AC19 心跳 | 正例：推进时钟后心跳刷新两级租约到当前+30秒；负例：租约过期时领取 `not_ready`、扫描标注 `lease_expired` 但不接管 | `test_heartbeat_refreshes_both_leases`、`test_expired_lease_blocks_claim_and_reports_without_takeover` |
| AC24 受理未派发可核对 | 扫描输出 `accepted_not_dispatched`、状态仍为 `accepted`、写入 `supervisor_scan` 执行记录、端口零调用 | `test_scan_records_accepted_not_dispatched_without_dispatching` |
| AC24 重启只核实不外发 | 过期 `running` 标记 `reconciling`+`restart_unconfirmed`、无outcome；`reconciling` 不被重开 | `test_scan_marks_unconfirmed_running_attempt_without_calling_the_runner`、`test_scan_does_not_reopen_reconciling_attempt` |
| AC19 数据库失联停止新调用 | 启动扫描即 `StorageUnavailable` 后 `halted=True`、`accepting=False`，`notify/claim/scan` 均拒绝，端口零调用 | `test_storage_loss_halts_supervisor_and_blocks_new_calls` |
| AC24 请求返回不取消 | 经 `supervisor_lifecycle` 进入后 `notify` 派发，调用方返回后Attempt仍 `running`、无取消标记 | `test_notify_dispatches_injected_port_and_outlives_the_caller` |
| AC24 关闭即停止领取 | 关闭后 `notify` 断言 `supervisor_stopped`，Attempt保持 `accepted` | `test_notify_after_shutdown_is_refused` |
| AC19 关闭超限保留未知 | 长任务在 `shutdown_seconds` 超限后取消本地调用，Attempt为 `reconciling`+`shutdown_unconfirmed`、无outcome、占位保留 | `test_shutdown_timeout_preserves_unknown_state_and_slots` |
| AC19 执行失败保守处理 | 端口抛业务错误时转 `reconciling` 且保留占位；正常返回则保持 `running` 不误判成功 | `test_runner_failure_settles_unknown_and_keeps_slots`、`test_runner_without_error_keeps_running_instead_of_cancelling` |
| AC24 不启动通用research图 | 未注入runner时 `analysis_runtime_unavailable` 且状态不变；源码静态断言无 `agents`/`service.service`/`astream` | `test_dispatch_without_injected_runner_refuses_to_run_a_graph`、`test_runtime_module_never_imports_the_generic_research_graph` |
| 售前入口寿命装配 | `background` 生命周期进入/退出各一次；`create_app` 暴露 `app.state.supervisor`、默认无runner、数据库不可达时 halted | `test_auth_app_enters_and_exits_its_background_lifecycle`、`test_create_app_assembles_supervisor_into_the_presales_lifespan` |
| 参数约束 | 心跳必须不超过租约一半，否则配置即拒绝 | `test_settings_require_heartbeat_well_inside_lease` |

未在本步验证（留到后续步骤）：真实PostgreSQL行锁顺序、跨进程双领取、提交到派发间进程崩溃、旧owner回归拒写、真实连接中断（任务12）；取消命令与排空（任务10）；业务恢复消费（任务11）；分析图与节点预算检查（2B-2）；REST/SSE与运行页（任务27—30）。

### A层：离线工程验证

当前worktree的`.venv`为空，验证命令复用同仓库已安装环境：`UV_PROJECT_ENVIRONMENT=C:/py_ai_project/biddesk-zq/.venv`、`UV_OFFLINE=true`、`--no-sync`。没有修改默认测试配置，没有新增skip/xfail，没有放宽断言或改超时。

开工基线（本次任务开始前）：`uv run --no-sync pytest tests/presales -q -rs` 为195通过、78跳过、0失败；`uv run --no-sync pytest -q` 为387通过、82跳过、0失败。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_runtime.py -q -rs` | 26通过、0失败、0跳过 |
| `uv run --no-sync pytest tests/presales -q -rs` | 221通过、78跳过、0失败；跳过数与基线一致 |
| `uv run --no-sync pytest -q` | 413通过、82跳过、0失败；跳过数与基线一致 |
| `uv run --no-sync ruff check src/presales/application/proposal_attempt_runtime.py src/presales/api/app.py src/presales/api/auth.py tests/presales/test_proposal_attempt_runtime.py` | 通过，0错误 |
| `uv run --no-sync ruff format --check`（同上四个任务文件） | 通过，4个文件已格式化 |
| `uv run --no-sync pyrefly check` | 通过，0错误；11项既有抑制、8条未展示警告 |
| `git diff --check` | 通过 |

新增用例与基线差额吻合（presales 195→221 为+26；全量 387→413 为+26），skipped 与 xfail 均无增量。

开发过程中首次专项为18失败、6通过，原因均为离线适配与断言口径：`NotReady` 导入位置、SQLite时间列按字符串返回、扫描结果 `attempt_id` 未转UUID、`running` 阶段应判 `attempt_busy` 而非 `not_ready`、已取消Attempt禁止写入执行记录。修正只限测试适配与一处业务分类（已取消的在途Attempt在扫描中归入 `awaiting_reconciliation`），未放宽断言、未改超时、未删反例。

### B层：本次未执行

未执行本机临时PostgreSQL验证，未使用 `--pg-bin`。本步清单未定义 `*_postgres` 专项用例，且任务9按清单由离线契约验证；因此真实行锁、跨进程占位、租约接管与崩溃时序均为「未执行」，不得以本次SQLite适配结论替代，对应任务12退出证据。

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。注入端口只证明监督器调度与隔离契约，不构成分析图、节点预算或真实模型能力证据。

## 4. 未实现项、风险与后续任务

- 未实现分析图与节点执行（2B-2）；当前注入端口为契约占位，领取后Attempt停在 `running` 等待真实执行者。
- 未实现取消命令与排空（任务10）、业务恢复消费（任务11）；`reconciling` 只由人工核实处理，本步不提供继续入口。
- 未接入REST/SSE与运行页（任务27—30）；监督器的 `notify`/`scan` 目前无HTTP入口，启动命令提交后需由后续命令显式唤醒。
- 单进程内以 `_pending`+`create_task` 派发，跨进程排他完全依赖数据库槽位与代次；离线用例不能证明该约束在真实并发下的表现（任务12）。
- 关闭期限内取消的是本地asyncio任务，不保证已外发的模型请求被撤回；对应状态保守保留为 `reconciling`。
- 心跳间隔与租约秒数为待验证技术参数（默认5/30秒），非实测SLA。
- 本次不改 `STATUS.md`、不改需求基线与设计文档结论。开工前已存在的未提交改动（`AGENTS.md`、`README.md`、`STATUS.md`、四份设计文档、`outbound.py`、`proposal_attempt_store.py` 及任务7/8新增文件）不属于本任务，未修改也未回滚。
