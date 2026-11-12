# 2B-1任务12交付记录：Proposal Analysis Attempt生命周期真实并发验证

日期：2026-11-12（Asia/Shanghai）。本记录覆盖任务12的B层真实PostgreSQL验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实模型未调用，分析图与四专家链未实现。

## 1. 实际交付范围

- 新增 `tests/presales/test_proposal_lifecycle_postgres.py`：8个真实PostgreSQL用例，全部通过真实子进程、真实进程终止、真实数据库连接与真实事务竞争取证，未使用异常注入或单进程模拟替代崩溃。
- 新增真实子进程worker（内嵌脚本，运行时落盘）：以独立Python进程执行启动命令或持有旧代次连接，覆盖双进程同时启动、受理后进程终止、旧owner迟到写入三类跨进程时序。
- 修复一个真实产品缺陷：`ProposalAttemptStore.declarations()` 的递归CTE 用 `jsonb_array_elements_text` 产生的text与 `direct_dependencies.upstream_id` 的uuid比较，真实PostgreSQL上直接报 `operator does not exist: uuid = text`。该路径此前只在SQLite适配下运行过，任务8交付时未被发现。修复为显式 `value::uuid`，并同步离线夹具适配。
- 修复一个真实产品缺陷：`ProposalAttemptStore.commit_result()` 成功落稿后不释放两级槽位，导致首次成功分析后全局执行位被永久占用、后续任何任务启动都返回 `global_busy`。修复为在落稿同一事务内调用 `release_slots()`，并以真实PG断言「成功后槽位为空且下一任务可启动」「落稿被拒时槽位保持占用」。
- `resume_proposal_attempt` 的租约参数化：新增 `lease_seconds integer` 参数并由监督器传入 `SupervisorSettings.lease_seconds`，函数内校验 `10..600`。

未新增迁移、HTTP/SSE入口、分析图或前端页面。

## 2. 用例与取证手段

| 场景 | 取证手段 | 断言 |
| --- | --- | --- |
| 双启动只受理一次 | 两个独立Python进程同时调用 `ProposalAttemptStart.start()` | 一方成功、另一方 `attempt_busy`/`global_busy`/`revision_or_request_conflict`；库中仅1条Attempt、1条任务槽位 |
| 受理后崩溃（非提交瞬间） | 子进程完成启动事务并返回后 `kill()` 真实终止 | Attempt保持 `accepted`；重启扫描输出 `accepted_not_dispatched`；不派发、不写终态。**不等同于「提交事务瞬间崩溃」**，该窗口未被覆盖 |
| 旧owner迟到写入 | 子进程持有旧代次连接，主进程完成真实续跑后再让其写入 | `invalid_proposal_execution_generation`；旧代次记录未落库 |
| 取消不提前释放槽位 | 真实取消命令 + `reconcile_cancelled` | `execution_stopped=False` 时另一任务启动 `global_busy`，槽位未释放；确认停止后才释放并可启动 |
| 续跑保留预算与修订次数 | 真实续跑函数 + 监督器领取 | 代次1→2、`reflection_revision` 与 `budget` 原值不变 |
| 撤权后禁止恢复 | 管理员撤销机会级授权后再请求恢复 | `not_found_or_not_allowed`；Attempt保持 `reconciling` |
| checkpoint缺损保持待核实 | 无checkpoint回执的恢复请求 | `recovery_blocked(checkpoint_missing)`；两级槽位仍占用 |
| 真实连接失联停止新调用 | 撤销 `CONNECT` 后新建连接池 | `StorageUnavailable`；监督器 `halted=True`、`accepting=False`；`scan`/`claim` 均拒绝 |

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC19/13/16 双启动一次受理 | 真实双进程竞争，仅一次成功，库中单Attempt单槽位 | `test_two_real_processes_start_only_one_attempt` |
| AC19 崩溃可核实 | 真实进程终止后仅扫描，`accepted_not_dispatched`，状态不迁移 | `test_hard_killed_start_is_only_reconciled_never_dispatched` |
| AC19 旧owner拒写 | 旧代次连接真实写入被数据库拒绝 | `test_old_owner_write_is_rejected_after_the_generation_advances` |
| AC19 取消与接管 | 取消后全局槽位保持占用；确认停止后才释放 | `test_cancel_keeps_global_slot_until_stop_is_confirmed` |
| AC19 重启保留预算 | 续跑后代次推进而预算/Reflection计数不变 | `test_resume_keeps_budget_and_reflection_counts` |
| AC19 撤权恢复禁止 | 撤权后恢复 `not_found_or_not_allowed` 且状态不变 | `test_revoked_actor_cannot_recover` |
| AC19 checkpoint缺损 | `checkpoint_missing` 且槽位保留 | `test_missing_checkpoint_keeps_the_attempt_reconciling` |
| AC19 数据库失联 | `StorageUnavailable` + halted，不继续外发 | `test_supervisor_halts_on_real_connection_loss` |
| AC19 成功后释放槽位 | 落稿成功后两级槽位为空、下一任务可启动；落稿被拒时槽位保持占用 | `test_successful_commit_releases_slots_and_unblocks_the_next_start`、`test_rejected_commit_keeps_slots_occupied`、`test_successful_commit_releases_both_slots_in_the_same_transaction`、`test_rejected_commit_leaves_slots_occupied` |

### A层：离线工程验证

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales -q` | 259通过、92跳过、0失败 |
| `uv run --no-sync pytest -q` | 451通过、96跳过、0失败 |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check`（本任务10个改动文件） | 通过，10个文件已格式化 |
| `uv run --no-sync pyrefly check` | 通过，0错误 |

skipped 由82升至96（+14）全部为 `pytest.mark.postgres` 用例在默认离线模式下的既有opt-in跳过（本任务新增12项 + 任务11新增4项）；未新增条件性skip、未新增xfail。

### B层：真实PostgreSQL（已执行）

环境：本机 PostgreSQL 18.6 临时回环集群（脚本自建、随机端口、专用 `biddesk_phase1_test` 库）、PGVector 0.8.6；全合成数据；未连接共享或客户库，未使用容器。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k proposal_lifecycle_postgres` | 8通过、0失败 |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k "proposal_attempt_start or proposal_attempt_context or proposal_attempt_runtime or proposal_attempt_recovery"` | 95通过、0失败（256 deselected） |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --full-suite --pytest-args -q -rs` | 542通过、5跳过、0失败（165.12秒） |

B层实际使用：仅本机临时回环实例与测试自建子进程；`kill()` 只作用于本测试创建的子进程。未触碰其他进程、共享库或客户数据。

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。Fake模型与注入端口只证明调度、隔离与预算契约，不构成真实Agent能力证据。

## 4. 未实现项、风险与后续任务

- 未实现分析图、四专家链与节点级预算检查（2B-2）；续跑后Attempt进入 `running` 仍无真实执行体。
- 未接入REST/SSE与运行页（任务27—30）。
- 真实多机/跨主机并发、网络分区与长时间稳定性未验证；本步覆盖单机多进程与真实连接中断。
- `resume_proposal_attempt` 的租约已参数化：函数签名为 `(…,intent_request uuid,lease_seconds integer)`，监督器传入 `SupervisorSettings.lease_seconds`，函数内校验 `10..600` 否则 `invalid_input`。
- 本次修复的 `declarations()` 类型缺陷属任务8遗留；建议后续在CI中以真实PG跑一次 `proposal_attempt_start` 相关门禁，避免同类SQL只在SQLite下"通过"。
- 本次未改 `STATUS.md` 的阶段验收结论。
- 开工前已存在的未提交改动（任务7—11相关文件及文档）不属于本任务，未回滚。

## 5. 执行记录保留策略（明确决策）

`presales.proposal_execution_records` 中的 `recovery-intent-*`、`supervisor-scan-*`、调用登记与节点产物记录一律只增不删，`guard_proposal_execution_record` 触发器也禁止改写 `completed`/`failed` 记录。这是有意的审计选择：恢复意图与消费回执必须可与最终结局对账，删除会使「重复恢复不重复消费」失去证据。

后果与边界：

- 表体积随恢复次数与节点数线性增长，二B阶段不做自动压缩；不在业务事务内删除任何执行记录。
- 若后续确需归档，必须新增独立的 `maintenance/` 命令，并保持「从审计表复制到归档表」而非原地删除；归档不得改变 Attempt、槽位与恢复判定结果。
- 当前没有配额或告警阈值；按单任务单Attempt、恢复次数有限的业务规模，二B阶段可接受，但需在二B退出评审时作为容量风险登记。

## 6. 防止「离线绿、真实红」的回归门禁

任务8 的 `declarations()` 类型缺陷只在 SQLite 下通过、在真实 PostgreSQL 上报 `uuid = text`，说明「离线用例全绿」不足以证明 SQL 可在目标库执行。为此登记以下门禁，供后续任务与CI使用：

| 门禁 | 命令 | 覆盖 |
| --- | --- | --- |
| Attempt 关键路径 | `uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --pytest-args -q -rs -k "proposal_attempt_start or proposal_attempt_context or proposal_attempt_runtime or proposal_attempt_recovery"` | 启动、预算、恢复意图与续跑函数在真实库可执行 |
| 生命周期时序 | `uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --pytest-args -q -rs -k proposal_lifecycle_postgres` | 并发、崩溃、取消排空、成功后释放槽位 |

执行约束：门禁必须在真实临时实例运行，SQLite 适配通过不计入；`-k` 档使用单场景超时，`--full-suite` 档用于合并前回归。新增任何 `jsonb`/`uuid`/`text` 混用的 SQL 时，必须至少有一条真实 PG 用例覆盖该语句，否则不予合并。
