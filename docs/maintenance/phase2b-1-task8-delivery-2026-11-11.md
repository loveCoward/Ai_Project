# 2B-1任务8交付记录：Presales Task启动Proposal Analysis Attempt

日期：2026-11-11（Asia/Shanghai）。本记录只覆盖任务8的离线工程实现和A层验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。真实并发与崩溃时序仍由任务12承接。

## 1. 实际交付范围

本次新增启动命令、启动事务写入口及任务状态最小权限迁移：

- `ProposalAttemptStart.start()` 在单个短事务内完成：架构师鉴权、对象授权、全局槽位行锁、任务行锁、回执重放判定、可启动状态判定、在途Attempt判定、关键/可选输入分类、容量与模式预检、启动范围校验、Attempt+两级占位+`analyzing`任务状态+运行投影+背景策略记录+命令回执+审计的原子写入。
- Attempt的`thread_id`由服务端按`attempt-<attempt_id>`生成，不接受外部传入；actor、任务、输入修订、启动范围、模型模式与配置身份均由服务端从命令与当前会话取值。
- 输入必需性以`presales.task_input_manifest`中不超过命令输入修订的最新记录为准；未分类输入不视为可选，直接拒绝启动。
- `migrations/versions/0007_proposal_attempt_start.py` 新增SECURITY DEFINER函数`presales.begin_proposal_analysis(actor,task)`，只允许`new/awaiting_supplementation/failed/analyzed_awaiting_submission → analyzing`，未向`presales_runtime`开放`UPDATE(state)`列权限。

沿用2B-0/2B-1任务7既有能力：`ProposalAttemptStore.create_attempt`的占位与唯一约束、`RunProjection`投影修订、`ProposalExecutionRecords`执行记录、`Policy`来源闭包与授权检查、`ProposalModel.preflight`模式与配置预检、`command_receipts`回执表、`audit.events`。

本次没有开放HTTP/SSE入口，没有实现监督器领取、心跳、取消、排空、恢复消费、分析图或节点调用，也没有实现容量超限后的截断或降级策略。

## 2. 流程与边界

架构师提交启动命令后，服务端先按固定锁序取得全局槽位与任务行锁，再判定回执。同`request_id`同参数的重复提交返回原受理身份，不新建Attempt；同`request_id`异参数返回`revision_or_request_conflict`；响应丢失后客户端必须用原请求身份重查，不能换键再启动。

任务已存在未终态Attempt时返回`attempt_busy`，全局槽位被其他Attempt占用时返回`global_busy`，两者都不排队、不写回执；该判定先于任务状态判定，以便重复提交得到稳定结论。任务修订或输入修订过期返回`revision_or_request_conflict`；`cancelled`任务返回`attempt_terminal`且不可复活；`analyzing/approved`等不可启动状态返回`revision_or_request_conflict`，状态保持不变。

关键材料未就绪、未解析或未分类返回`not_ready`；可选材料缺失不阻塞启动，只在受理结果`missing_optional_sources`中列出。关键输入总字符数超过配置容量返回`invalid_input`，不做静默截断。关键输入不在启动范围内返回`not_found_or_not_allowed`。模型模式未显式选择、配置身份不一致、模式为`disabled`或缺少显式Fake时拒绝启动。

离线测试使用SQLite适配、合成UUID与显式Fake模型；`test_proposal_attempt_start.py`中的`_begin_analysis`是本测试内对`presales.begin_proposal_analysis`的模拟，用于验证命令契约。它不证明PostgreSQL SECURITY DEFINER函数的真实权限、行锁、并发双启动或崩溃时序；这些证据须由任务6/12在真实临时实例取得。

## 3. 验收与验证

| 验收范围 | 实际验证 |
| --- | --- |
| AC19 合法状态启动 | `new/awaiting_supplementation/failed/analyzed_awaiting_submission`四态各一个正例，断言Attempt、两级占位、`analyzing`状态、投影、背景策略记录、回执与审计同事务产生 |
| AC19 非法状态与终态 | `analyzing/approved`断言`revision_or_request_conflict`且状态未迁移；`cancelled`断言`attempt_terminal`且不建Attempt |
| AC19 全局忙与重复启动 | 全局槽占用断言`global_busy`且无回执无Attempt；同任务第二次启动断言`attempt_busy`且Attempt数仍为1 |
| AC19 关键材料与背景必需性 | 未就绪必需材料、未分类材料均断言`not_ready`；`unconfirmed`背景在契约层构造即被拒绝；可选材料缺失断言列入`missing_optional_sources`而不阻塞 |
| AC19 容量与模式预检 | 超容量断言`invalid_input`且不静默截断；等于容量边界为通过；`disabled`模式、配置身份不一致、缺少模型客户端分别拒绝 |
| AC19/AC13 同键同参复用与异参拒绝 | 同命令重放返回完全相同的受理结果且Attempt数不变；同`request_id`改配置身份断言`revision_or_request_conflict` |
| AC04/AC05 授权与范围 | 非架构师、停用主体、`reviewer`角色均`not_found_or_not_allowed`；关键输入不在`start_scope`内同样拒绝 |
| AC13 输入修订检查 | 任务修订或输入修订过期断言`revision_or_request_conflict`且状态未迁移 |
| 迁移最小权限 | 迁移静态断言：SECURITY DEFINER、固定`search_path`、advisory lock、允许状态枚举、`ROW_COUNT`校验、仅`GRANT EXECUTE`、无`GRANT UPDATE(state)`、无`GRANT ALL` |

### A层：离线工程验证

当前worktree的`.venv`为空，验证命令复用同仓库已安装环境：`UV_PROJECT_ENVIRONMENT=C:/py_ai_project/biddesk-zq/.venv`、`UV_OFFLINE=true`、`--no-sync`。没有修改默认测试配置，没有新增skip/xfail，没有放宽断言或改超时。

开工基线（本次任务开始前，未含本任务文件）：`uv run --no-sync pytest tests/presales -q -rs`为170通过、78跳过、0失败；`uv run --no-sync pytest -q`为362通过、82跳过、0失败。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_start.py -q` | 25通过、0失败、0跳过 |
| `uv run --no-sync pytest tests/presales -q -rs` | 195通过、78跳过、0失败；跳过数与基线一致 |
| `uv run --no-sync pytest -q` | 387通过、82跳过、0失败；跳过数与基线一致 |
| `uv run --no-sync ruff check src/presales/application/proposal_attempt_commands.py src/presales/infrastructure/proposal_attempt_store.py migrations/versions/0007_proposal_attempt_start.py tests/presales/test_proposal_attempt_start.py` | 通过，0错误 |
| `uv run --no-sync ruff format --check`（同上四个任务文件） | 通过，4个文件已格式化 |
| `uv run --no-sync pyrefly check` | 通过，0错误；11项既有抑制、7条未展示警告 |
| `uv run --no-sync alembic heads` / `alembic history` | 单一head `0007_proposal_attempt_start`，前驱`0006_proposal_attempt`，无分支 |
| `git diff --check` | 通过 |

开发过程中先有19项任务8用例失败，原因均在测试适配而非业务断言：SQLite缺少`pg_advisory_xact_lock`、`jsonb`/`rowcount`参数签名、`input_manifest`未按JSON解码、夹具未把任务输入纳入`start_scope`、UUID重复转换。修复只限测试适配层；修复后25项通过。期间`Conflict`与`AttemptBusy`的判定顺序因重复提交语义被调整一次，属实现修正，未放宽断言。ruff首轮报告1项未使用导入与2个文件待格式化，已修正后0错误。

### B层：本次未执行

未执行本机临时PostgreSQL验证，未使用`--pg-bin`。因此`0007`迁移未在真实实例执行，SECURITY DEFINER函数、`ROW_COUNT`分支、`presales_runtime`真实执行权限、行锁顺序、双启动竞争、提交到派发间崩溃与旧owner拒写均未验证，对应任务6既有门禁与任务12的退出证据。不得以本次SQLite模拟结论替代。

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。模型相关断言只覆盖模式与配置预检契约，不构成真实模型能力或容量证据。

## 4. 未实现项、风险与后续任务

- 未接入REST/SSE与运行页（任务27—30）；本次启动命令没有HTTP入口，契约通过不代表接口可用。
- 未实现监督器领取、心跳、关闭与派发（任务9）；受理后Attempt停留在`accepted`，没有执行者，也没有派发时序证据。
- 未实现取消、排空与恢复消费（任务10/11）；`attempt_busy`与`global_busy`的释放路径尚未建立。
- 容量预检当前只累计关键输入的解析字符数，未计入模板、知识检索结果和输出预算；真实上下文容量仍待任务26探针。
- 背景策略以已完成执行记录锁定，但只记录必需性取值，没有实现任务13的Opportunity只读来源读取与失败分类。
- 离线`_begin_analysis`模拟与真实函数行为可能在触发器、权限或并发语义上存在差异，须由任务6/12真实实例验证。
- 本次不改`STATUS.md`，不修改需求基线或设计文档结论。开工前已存在的未提交改动（`AGENTS.md`、`README.md`、`STATUS.md`、四份设计文档、`outbound.py`及任务7新增文件）不属于本任务，未修改也未回滚。
