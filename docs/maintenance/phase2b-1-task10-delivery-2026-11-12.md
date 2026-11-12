# 2B-1任务10交付记录：Presales Task取消与Proposal Analysis Attempt排空

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务10的离线工程实现和A层验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实PostgreSQL取消/落稿竞争、跨进程排空与进程崩溃时序仍由任务12/24承接。

## 1. 实际交付范围

- `ProposalAttemptCancel` 新增架构师取消命令：先在短事务内复核架构师角色、任务授权、任务修订、指定Attempt归属及Attempt未终态，再持久写入Attempt `cancelled_at`、转`reconciling`、递增运行修订，随后调用数据库最小权限函数把任务转为`cancelled`并递增任务修订。两级槽位在取消回执中保留。
- `0008_proposal_attempt_cancel` 新增`presales.cancel_proposal_task(uuid,uuid,bigint)`：`SECURITY DEFINER`、固定`search_path`、共享授权锁、架构师与任务授权重验、`FOR UPDATE`任务行、修订匹配、拒绝`approved/completed`、已取消任务和非法状态；只授予运行时函数执行权，不授予`tasks.state`的任意UPDATE权。
- 取消命令支持稳定request ID幂等：同键同参返回原回执，同键异参返回`revision_or_request_conflict`；重复取消已取消任务返回同一业务结果，不再改终态。
- 取消先/落稿先两序：取消先提交时迟到`commit_result`返回`attempt_terminal`且不写候选；落稿先提交时允许按P01取消未批准任务，保留已形成草稿/结果指针和成功Attempt。
- 取消后的旧checkpoint在`ProposalCheckpointFence.check()`中识别`cancelled_at`并返回`attempt_terminal`；槽位已释放时先识别取消终态，再报告fence冲突。
- 监督器新增`cancel()`与`reconcile_cancelled()`：取消只停止本进程待派发/本地执行，不自动释放槽位；只有在调用方明确`execution_stopped=True`且没有`registered/unknown`在途调用时，才条件结束Attempt为`cancelled`并释放两级槽位。存在未结调用或未确认停止时保持`reconciling`。
- 未新增HTTP路由、SSE、前端页面、分析图、真实模型调用或业务恢复入口。

## 2. 流程与边界

取消回执表示“任务已取消且旧执行已失效”，不表示“在途调用已排空”。取消事务完成后任务立即不可重启，旧Attempt的迟到落稿、执行记录写入和checkpoint提交均被拒绝；但全局槽位继续被该Attempt占用，直到执行端确认停止且无未结调用。无法确认时保持`reconciling`和槽位，不允许新分析启动。

`approved`/`completed`任务拒绝取消并返回`cancel_not_allowed`；已取消任务再次取消返回既存取消结果，重新启动返回`attempt_terminal`。真实HTTP断开不是取消，本步也没有通过HTTP触发取消的入口。

离线测试使用SQLite适配（剥离`FOR UPDATE`/`FOR SHARE`、转换`jsonb`与`now()`、显式模拟取消函数），Fake时钟与Fake runner只证明状态、权限、幂等、失败与排空顺序契约。它不证明PostgreSQL行锁顺序、真实事务竞争、跨进程租约接管或进程崩溃；这些证据须由任务12/24取得。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC19/13 取消合法路径 | 正例：任务`analyzing`、Attempt`running`取消后任务`cancelled`、Attempt`reconciling+cancelled_at`、回执`drain=reconciling`、两级槽位保留、审计写入 | `test_cancel_persists_task_attempt_and_receipt_but_keeps_slots` |
| AC19/13 取消先/落稿先 | 正例：落稿先成功后仍可取消且保留`result_revision_id`/成功outcome；反例：取消先时迟到`commit_result`断言`attempt_terminal`且outcome为空 | `test_commit_before_cancel_preserves_draft_and_marks_task_cancelled`、`test_cancel_before_commit_blocks_late_result` |
| AC19 批准后拒绝取消 | 反例：`approved`、`completed`均断言`cancel_not_allowed`，任务状态和Attempt取消标记不变 | `test_approved_or_completed_task_refuses_cancel` |
| AC19/13 幂等与同键异参 | 正例：同request ID同参重放返回同一结果且任务修订只+1；反例：同键异参断言`revision_or_request_conflict`且修订不变 | `test_cancel_is_idempotent_and_rejects_parameter_change` |
| AC19/13 修订与归属 | 反例：旧任务修订、非本任务Attempt均拒绝，任务保持`analyzing` | `test_cancel_rejects_stale_revision_and_foreign_attempt` |
| AC04/05/19 权限 | 反例：非架构师、无任务授权主体均断言`not_found_or_not_allowed`且任务不迁移 | `test_non_architect_or_foreign_actor_cannot_cancel` |
| AC19 已取消不可复活 | 正例：重复取消返回既存结果；反例：取消后启动路径由任务8测试断言`attempt_terminal`且不建新Attempt | `test_cancelled_task_cannot_be_revived_by_start_or_cancel`、`tests/presales/test_proposal_attempt_start.py::test_cancelled_task_cannot_be_started_and_stays_cancelled` |
| AC19 停止调度 | 正例：`accepted`取消后监督器`claim`断言`attempt_terminal`且runner零调用 | `test_cancel_before_notify_does_not_dispatch` |
| AC19 未知在途继续占位 | 正例：存在`registered`调用时`reconcile_cancelled(execution_stopped=True)`仍为`reconciling`且槽位保留 | `test_drain_waits_for_open_call_and_keeps_slots` |
| AC19 排空完成才释放 | 正例：`execution_stopped=False`保持`reconciling`与槽位；确认停止且无未结调用后转`finished/cancelled`并释放两级槽位 | `test_drain_finishes_and_releases_slots_only_after_execution_stops` |
| AC19/13 迟到checkpoint隔离 | 反例：取消后checkpoint fence断言`attempt_terminal` | `test_cancelled_attempt_rejects_new_checkpoint` |
| AC19 最小权限迁移 | 静态断言：`SECURITY DEFINER`、固定`search_path`、仅函数执行授权、无`GRANT UPDATE(state)`或`GRANT ALL` | `test_migration_grants_only_a_cancel_function` |

未在本步验证（留到后续步骤）：真实PostgreSQL取消与落稿事务竞争、跨进程槽位占用与租约接管、进程崩溃、真实连接中断（任务12/24）；HTTP断线/关页与浏览器取消入口（任务27—30）；真实模型请求撤回或计费停止（供应商能力，不在二B承诺内）。

### A层：离线工程验证

开工基线（本次任务开始前）：`uv run pytest -q` 为413通过、82跳过、0失败。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run pytest tests/presales/test_proposal_attempt_cancel.py -q -rs` | 13通过、0失败、0跳过 |
| `uv run pytest tests/presales/test_proposal_attempt_cancel.py tests/presales/test_proposal_checkpoint_contract.py -q -rs` | 17通过、0失败、0跳过 |
| `uv run pytest tests/presales -q -rs` | 234通过、78跳过、0失败；跳过数与基线一致 |
| `uv run pytest -q` | 426通过、82跳过、0失败；跳过数与基线一致 |
| `uv run ruff check`（本任务7个文件） | 通过，0错误 |
| `uv run ruff format --check`（本任务7个文件） | 通过，7个文件已格式化 |
| `uv run pyrefly check` | 通过，0错误；11项既有抑制、8条未展示警告 |
| `git diff --check` | 通过 |

新增用例与基线差额吻合：presales 全量新增13项，全量413→426；skipped保持82，无xfail增量。

开发过程中专项首次为12失败/1通过，后续为11失败/2通过、8/5、11/2，原因依次是测试夹具未附`iam/audit/presales` schema、缺少`presales.resources`、重复取消与落稿先完成的状态收窄、SQLite时间字符串与UUID类型适配；修复范围限于测试夹具、取消命令的幂等/落稿先分支及checkpoint终态判断。未放宽断言、未改超时、未新增skip/xfail、未删除既有失败用例。

### B层：本次未执行

未执行本机临时PostgreSQL验证，未使用`--pg-bin`。本步按清单由离线契约验证；真实取消/落稿竞争、跨进程排空与崩溃时序均为「未执行」，不得以SQLite适配结论替代，对应任务12/24退出证据。

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。Fake runner只证明监督器停止调度和排空接口，不构成分析图、真实模型能力或供应商撤回证据。

## 4. 未实现项、风险与后续任务

- 未实现取消REST命令和前端取消按钮（任务27—30）；当前取消能力只有应用服务/监督器接口。
- 未实现任务11的重启核实、受控续跑与恢复消费；`reconciling`只能由本步的`reconcile_cancelled`在确认停止后收束，不能重新推进分析。
- 未验证真实PostgreSQL取消与落稿事务串行化、跨进程双取消、槽位释放竞态和进程崩溃；离线测试不能证明真实数据库约束/锁顺序。
- 取消不承诺供应商撤回已发送模型请求或停止计费；只保证本系统的迟到结果不写候选/旧checkpoint并保持保守占位。
- `execution_stopped`由调用方/执行端注入，本步没有跨进程执行存活探测；无法确认时必须保持`reconciling`，不得默认传`True`。
- 本次不改`STATUS.md`的既有阶段验收结论，只按实际代码更新“任务10已实现离线取消/排空、真实竞争仍未验证”的事实边界。
