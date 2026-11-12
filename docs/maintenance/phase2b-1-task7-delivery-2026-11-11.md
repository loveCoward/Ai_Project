# 2B-1任务7交付记录：Proposal Analysis Attempt授权与调用预算

日期：2026-11-11（Asia/Shanghai）。本记录只覆盖任务7的离线工程实现和验证，不代表2B整体完成、阶段验收、部署批准或真实Agent能力达标。

## 1. 实际交付范围

本次新增Attempt运行上下文和显式模型传输适配：

- `ProposalAttemptContext` 在每次来源读取、模型调用登记、调用结果接收和后续持久化前重验Attempt身份、actor、任务输入修订、执行代次、任务状态、取消标记、任务槽位、全局槽位、来源闭包及当前授权。
- 授权失效会将Attempt持久标记为`authorization_invalid`并拒绝后续操作；恢复授权不会复活该Attempt。
- 主动运行预算通过Attempt执行记录累计。活动时间按区间并集计算，并行区间不会重复扣除；重建上下文后从执行记录恢复；尚未核实的调用保持未知并阻止盲目重试或结束运行。
- 模型调用按配置的模式、配置身份、来源声明和接收端执行。Fake必须显式注入；未配置、模式不一致、缺少Fake或真实接收端身份不匹配时拒绝。真实模式失败不会切换到Fake。
- 模型和调用参数仅以摘要登记；观测接口只允许固定元数据字段，不接收正文、URL查询参数或秘密。

沿用2B-0已有的`ProposalExecutionRecords`、Attempt身份/代次和`AttemptBudget`。本次没有新增迁移，没有接入启动命令、监督器、取消命令、完整恢复消费、REST、SSE或分析图。

## 2. 流程与边界

执行者先用服务端Attempt身份创建授权边界。边界确认任务仍处于分析中、Attempt仍处于当前代次、双层占位仍属于该执行者，并重新读取当前授权和来源闭包。来源超出启动范围、来源未就绪、授权撤销、任务取消或Attempt进入终态都会拒绝操作；撤权发生后，迟到的模型结果不能进入后续流程。

通过边界后，运行上下文登记主动区间，再按节点登记模型或MCP调用及其单次截止时间。模型默认上限为120秒，MCP默认上限为10秒，主动总预算为600秒，技术重试使用已有配置的初次后最多2次限制。执行区间和调用记录写入已有Attempt执行记录，重新创建上下文后继续使用原记录计算剩余预算。

本次离线测试使用合成UUID、合成来源、Fake时钟、SQLite适配和HTTP MockTransport。SQLite适配显式移除行锁及advisory lock，仅用于A层契约验证，不证明PostgreSQL事务锁、跨进程并发、真实连接中断或进程崩溃恢复。纯计算节点可调用`remaining()`检查主动截止，持久化方可在`boundary()`事务中重验并写入；这些接口尚未接入后续分析图。未知旧区间计入登记截止上界，仍阻断新活动区间，只有任务11核实后才能决定恢复，不能以保守扣时替代核实。

## 3. 验收与验证

| 验收范围 | 实际验证 |
| --- | --- |
| AC04/05/22授权和来源边界 | 正常来源、不同来源、来源收紧、撤权、停用主体、越权来源、任务取消、伪造thread/代次均有反例；21项任务7专项通过 |
| AC19预算和失败 | 120秒模型、10秒MCP、600秒主动预算、初次后最多2次技术重试、超时、未知结果、非法模型身份、配置缺失和真实失败不切Fake均有断言 |
| 重启与重放 | 新建上下文后预算与调用记录仍在；未知调用拒绝盲重试；授权失效Attempt进入终态后不能由恢复授权复活 |
| 观测和秘密边界 | 只接受固定状态、耗时、模式等元数据；正文、秘密和带查询参数地址不会进入观测元数据或执行记录 |

### A层：离线工程验证

当前worktree的`.venv`为空，首次`uv run --no-sync ruff format ...`与`uv run --no-sync pyrefly check`未能启动（工具不存在，退出2）；未安装依赖。后续命令均在本worktree根目录运行，进程环境设置`UV_PROJECT_ENVIRONMENT=C:/py_ai_project/biddesk-zq/.venv`复用同仓库已安装环境，设置`UV_OFFLINE=true`并使用`--no-sync`。没有修改默认测试配置或设置全局Fake开关。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_context.py -q -rs` | 21通过、0失败、0跳过 |
| `uv run --no-sync pytest tests/presales -q -rs` | 170通过、78跳过、0失败；5条既有警告 |
| `uv run --no-sync pytest -q -rs` | 362通过、82跳过、0失败；18条既有警告 |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check src/presales/application/proposal_attempt_context.py src/presales/infrastructure/proposal_model.py src/presales/infrastructure/outbound.py tests/presales/test_proposal_attempt_context.py` | 通过，4个任务文件已格式化 |
| `uv run --no-sync pyrefly check` | 通过，0错误；11项既有抑制、7条未展示警告 |
| `git diff --check` | 通过 |
| `uv run --no-sync ruff format --check` | 失败，11个既有任务外文件需要格式化，168个文件已格式化；没有修改任务外文件 |
| `uv run --no-sync pymarkdown scan docs/maintenance/phase2b-1-task7-delivery-2026-11-11.md` | 通过，0违规 |

全量离线测试中的82项跳过为77项需要显式临时PostgreSQL授权的用例、4项Docker用例和1项Windows符号链接权限用例；它们不计入通过，也没有通过修改默认配置来绕过。最终全量运行362通过、82跳过、0失败，30.42秒。全量格式检查已实际执行，报告的11个任务外文件为`src/memory/postgres.py`、`src/presales/access/jwt.py`、`policy.py`、`application/auth.py`、`service.py`、`domain/contracts.py`、`infrastructure/config.py`、`database.py`、`maintenance/service.py`、`src/run_service.py`和`src/service/service.py`，本次未修改它们。

首次任务7专项为6失败、13通过：SQLite的`CAST(:id AS uuid)`按数值处理，导致来源声明查询为空。修正仅限测试SQL适配，不改变业务断言；修复后19通过，增加传输超时与模型身份错误负例后21通过。首次类型检查2错误为新增代码的Literal签名不准确，补充类型后0错误。失败没有通过删例、跳过、改超时或换模式消除。

任务7只涉及5个文件：新增`proposal_attempt_context.py`（303行）、`proposal_model.py`（124行）、专项测试（351行）和本文；修改`outbound.py`增加20行固定观测接口。代码及对应测试共798行，未修改迁移、实施清单或其他任务代码。开工前7份文档共31行新增、29行删除保持原范围，不计入本任务差异。

### B层：本次未执行

未执行本机临时PostgreSQL验证。本次没有使用`--pg-bin`或`--full-suite`，因为任务7按清单由离线契约验证，真实数据库竞态、事务边界、跨进程占位和崩溃恢复由任务12承接。不能用本次SQLite适配和Fake时钟结果替代这些证据。

### C层：本次未执行

未调用云端模型API、真实客户数据或任何外部写入/发布。真实模型能力、结构化输出和费用行为不属于本次任务证据。

## 4. 未实现项、风险与后续任务

- 任务7未实现任务8的启动命令和原子受理，因此不能从本记录推导合法启动流程已完成。
- 未实现任务9—11的监督器、取消、完整恢复消费和重启核实；未知调用只在本次上下文契约中被保守阻断。
- SQLite适配只用于离线测试。真实PostgreSQL行锁、授权修订并发、旧代次拒写、连接中断和进程崩溃恢复未验证，对应任务12仍是必要退出证据。
- 未执行真实模型探针；Fake模型通过只证明授权、失败、预算和恢复流程，不证明真实模型能力、上下文容量或业务语义质量。
- 本次不改`STATUS.md`。开工前已存在的用户未提交修改保留，不计入本任务文件清单和验证结论。
