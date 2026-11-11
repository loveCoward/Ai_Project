# Phase Two B：Proposal Analysis Attempt实施任务清单

## 状态与版本

- 本文档版本：v1.2（已完成AI技术复评）。
- 状态：已评审（AI技术复评通过；未执行，不构成实施授权）。
- 适用范围：覆盖【2B-0、2B-1、2B-2、2B-2链路可用后的早期真实探针、2B-3及二B退出评审；上游第1—9节中的二B职责】；不覆盖【二A缺失能力的补齐实施、阶段三正式编辑/澄清确认/审阅审批/人工审批恢复、阶段四External Write、阶段五完整导出/Compose交付/冻结验收】。
- 生成依据：2026-11-10修订的[阶段二B方案](../specs/阶段二B-Agent分析工作流设计方案.md) v0.1-review-draft全文，重点第4—7节、第8.1/8.2节及第9节；[总体架构设计](../specs/总体架构设计.md) v0.2-review-draft第3、6—8节；需求v0.1的AC07—AC10、AC19、AC21、AC27，另承接AC04/05/06/13/16/22/24中二B相关工程边界，均非完整AC验收结论。
- 生成日期：2026-11-10（Asia/Shanghai）。
- 本次修订日期：2026-11-11（Asia/Shanghai）。
- 技术复评日期：2026-11-11（Asia/Shanghai）；评审范围和结论见文末第9节。该状态仅适用于本实施清单，不改变上游方案的待评审状态或任何实施期门禁。
- 批准边界：上游二B仍待评审；本清单是作者拆分建议，不把监督器、路径、DTO或具体协议建议升级为已批准设计。所有任务当前均未执行；编制清单不授权实施、部署或真实调用。
- 评审整改依据：保留v1.1所记的2026-11-10只读评审R-01—R-20摘要；本次另依据2026-11-11本聊天的v1.1复评（项目约定轴3条、方案一致性轴3条，去重后4类问题）及用户“修复和完善”指令。修复重复确认门槛、准备任务自阻塞、并行声明冲突和启动命令缺失，不修改上游规则或把作者建议升级为批准。原R-01—R-20完整意见未随本文件附入，本次不声称逐条关闭；见第7、8节的规则与修订记录。
- 输出位置：用户指定`_docs/superpowers/plans`，但该目录不存在，同时要求“不建目录”。本次不创建目录，单文件落入已有`docs/superpowers/plans/Phase-Two-B-Implementation-v1.md`；迁往指定位置保留为路径缺口，不静默声称路径已满足。
- 依据指纹：二B方案SHA256为`7128DE782BAA77250952CADB7D64A1600C927579C32995BF5265F6039B6135D6`；架构方案SHA256为`2F43D5DC8D8E2A2FCCD994CA01308251B668EF405BB4F4BEBE4C380807D51DE8`。版本号相同时以本次指纹区分修订。

## 1. 开工定位、术语与阻塞条件

已全文阅读二B方案、`AGENTS.md`、`CLAUDE.md`、`GLOSSARY.md`、`STATUS.md`，并核对需求AC表、双入口接入设计、总体架构第7.1节及相关TD。本次只静态读取和生成本文件，不运行清单中的命令。状态文档不替代源码证据，目录存在不代表能力就绪。

开始时工作区已有两份未提交修改：`docs/superpowers/specs/总体架构设计.md`、`docs/superpowers/specs/阶段二B-Agent分析工作流设计方案.md`。本清单以上述工作区版本为输入，不覆盖、提交、回滚或再次修改它们。

任务名沿用GLOSSARY的Proposal Analysis Attempt、Presales Task、Proposal Package、Proposal Draft、Evidence、Evidence Snapshot、Opportunity、Requirement、Coverage Status、Product-fit Coverage、Risk、Clarification Question、Golden Dataset、Agent Capability Evaluation。槽位、fencing、SSE属于技术机制，不作为新增业务对象；新模块以对应业务对象命名，不改写术语表。

| 依赖代号 | 本次静态事实及路径 | 对清单的约束 |
| --- | --- | --- |
| D0：评审及实施范围 | 二B方案标为待评审，`STATUS.md`未记录二B实施批准 | 所有实现任务均依赖【上游方案评审接受及对应实施范围获授权】；任务1可先作只读评审准备 |
| D1：二A-2 | `src/presales/`现有access/api/application/**domain**/infrastructure/materials/knowledge/indexing/maintenance等模块（本次核对；v1 曾漏列`domain`）；无受限在线检索及Product Catalog MCP业务实现 | 依赖【二A-2受限检索与只读Product Catalog MCP完成并提供真实API/权限/稳定Evidence ID证据】；不得私建第二套检索/ACL |
| D2：二A-3 | `frontend/src/main.tsx`、`api.ts`仅登录/身份/按UUID受限读取；无材料/知识业务页面 | 依赖【二A-3页面及二A认证/到期/越权真实API联调完成】；fixture不解除阻塞 |
| D3：二A早期探针 | 架构第7.1节已确认路线，但当前未见可作为本次执行结果的报告；开发样例未证明就绪 | 依赖【二A探针经授权执行并形成报告】；基本不可行/硬门禁失败阻断相关能力，语义差距登记后可继续，未执行不算通过 |
| D4：业务存储基础 | `Policy.source_closure()`已有来源闭包（`access/policy.py`）；`PresalesService`已有命令回执/修订；`DraftContent`只是阶段一草稿，`analysis_attempt_id`固定写在手工草稿落库body的`identity`块并取`None`（`application/service.py`），不是`DraftContent`字段；`_task()`限制`state=="new"` | 可复用基础，不能原样用手工草稿入口落分析结果；实际数据库状态未经本次验证 |
| D5：工作流存储 | `src/memory/postgres.py`是通用适配；售前兼容入口在`api/app.py`中使用SQLite saver与内存store，无售前Attempt/checkpoint绑定 | 由任务5交付适配、任务6取得真实提交边界证据；它是二B内部交付门禁，不是任务1或任务6自身的先验就绪条件；依赖【5、6完成后】才能开启受控续跑 |
| D6：本机数据库 | `migrations/versions/0004_knowledge.py`要求PGVector 0.8.6；脚本支持真实临时回环实例，环境可用性未核验 | 所有真实数据库任务需专用实例、核定主版本/扩展和运行/维护角色；默认离线测试跳过不能算通过 |
| D7：真实探针条件 | 10个开发/20个验收样例是批准的组成，不代表已制作；模型、端点、次数/预算和调用授权尚未就绪 | 任务25可准备所选开发样例、送复核及执行入口，不要求这些成果预先存在；任务26才依赖【所选数据复核、专项计划固定及真实调用明确授权完成后】执行，不使用验收标签调优 |
| D8：浏览器集成 | `frontend/package.json`只有dev/typecheck/build/preview，无浏览器E2E；`scripts/e2e_ui_tests.py`是Streamlit脚本 | 由任务31建立React专用入口/锁定环境要求；入口和真实浏览器尚未就绪不阻塞31的离线准备。任务32依赖【31交付及真实浏览器/售前HTTP/PostgreSQL就绪后】执行，不能复用Streamlit证据 |

所有实现任务当前还受D0阻塞；任务表中的“依赖【D1/D2/D3完成后】”表示额外业务前置，不能据空闲任务或Fake接口声称可正式开工。按二B方案第1节，任务2起须经任务1核对D0明确授权、D1/D2/D3真实证据和D4可复用基础。D5/D7/D8的二B内部成果只登记负责人、产出任务及未来使用门禁，未完成不反向阻塞任务1；D6实例可用性在任务6等真实执行前核实，不作为离线任务2—5的额外门槛。只读研究、待审契约草案可先准备，不等于实现获准。

任务1核对已有批准/授权和可复核证据，不产生新的方案批准或实施授权；满足已定前置后可按已授权范围推进，不新增“签署人尚未指定”的重复放行门槛。任务26按上游已确认的三类规则记录结论与依赖影响：基本不可行先解决再推进依赖工作；语义差距留痕后可继续已授权工程；硬门禁失败立即停止相关执行和推进，不能等待再次签署才阻断或自行降低严重度。分类有争议、拟改变业务规则/授权范围或采用降低能力的备选方案时才提请用户裁决；真实模型调用仍须单独明确授权。技术证据允许独立复核，但复核流程不替代既定规则，也不新增逐次审批。

## 2. 数量、粒度与验证约定

共33项，编号1—33连续，每项只有一个主归属增量。原方案“2B-2链路可用后探针”的25、26主归属2B-2，第4节探针行是复用视图，不构成第二归属、不新增阶段编号；“二B退出评审”为原方案独立收束行。编码任务预计一次审查涉及1—2个业务/基础设施模块，连同对应测试约300—800行；模块内强耦合事务、取消与排空不拆成相互不完整的任务。文档审查和纯执行取证不按代码行数填充。行数是估计，若开工实测超过800行，应先修订本清单并追加修订记录再拆分，不能借大任务无限扩范围。

涉及模块/文件均为仓库根目录相对路径。标“拟新增”的文件当前不存在，是后续交付建议，不是本轮创建指令；本轮已为多数拟新增路径给出默认位置，开工核对后记入交付记录，仍未给出默认值的（如契约导出命令）在交付时补记。测试路径同样属于该任务拟新增交付物，表中pytest命令是文件交付后的目标命令，不声称今天可运行；不能因文件不存在或没有收集到测试而记通过。未列为拟新增的路径本次已通过目录/源码检查。

验证列以“按项目约定填写：<命令>”给出目标命令。“按项目约定填写”表示该命令由`AGENTS.md`常用命令派生，项目约定后续变化时以变化后的约定命令为准；但不得替换为更宽松的证据（跳过、fixture 代跑、改用 SQLite、删除负例、人工目视代替断言）。命令只供未来执行，本轮最多运行文档格式检查。所有离线任务使用注入的Fake模型/存储/时钟/传输，不启动真实数据库、浏览器或MCP子进程，不出站；结果只证明流程契约。Fake模型必须显式选择，`pyproject.toml`里的测试Key不是调用授权。离线测试不得隐式加载默认联网Embedding或通用research图。

| 验证分类 | 实际约定及证据要求 |
| --- | --- |
| 离线Python | 表中指定`uv run pytest tests/presales/test_*.py -q`；对应新文件须自行不依赖真实数据库fixture，不用跳过代替成功 |
| 真实PostgreSQL | 使用现有`uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --pytest-args ...`；用`-k`筛选拟新增测试的唯一名称；必须有实际收集/执行条目和非跳过结果。脚本按用途分档核定的 pytest 子进程上限（单场景`-k`档与`--full-suite`全量档，由任务6交付），当前源码仍是单一180秒硬编码（`scripts/test_presales_postgres.py:173`）；未分档前`--full-suite`档只作为目标命令，不得宣称已取证，也不得靠延长业务10分钟预算掩盖超时 |
| 前端静态 | 工作目录`frontend/`执行`npm run typecheck`、`npm run build`；依赖安装按项目约定`npm ci`仅在未来任务需要时进行，不在本轮执行 |
| 新验证入口 | 任务25/31必须交付文中拟议脚本及其CLI测试后，任务26/32的命令才可运行；当前无售前探针/React浏览器命令，不伪称已有npm test/e2e脚本 |
| 统一工程检查 | 编码任务完成其范围内测试后，执行`uv run ruff check <修改路径>`、`uv run ruff format --check <修改路径>`；影响共享模块时按项目约定执行完整`uv run pytest`及`uv run pyrefly check`，由任务24汇总。真实数据库回归与离线回归分开报告 |
| 文档证据 | 退出证据记录代码修订、命令/环境/模式、收集/通过/失败/跳过、产物身份和缺口；凭据不入命令、日志、报告。没有实跑报告的项目保持未验证 |

各任务可单独审查其改动，但代码审查通过不等于真实集成验收通过。任务4/5/8—11等的真实保证分别由6/12/24承接，表中注明的Stub测试不替代该门禁。33只核对报告，不重复执行任何工程或真实服务命令。

### 2.1 环境矩阵与“外部依赖”列口径

第3节“外部依赖”列固定以“真实模型/真实数据库/浏览器E2E”三元组记录该任务触及的真实环境，差异另注。任一口径为“是”即该任务不能只交离线证据；为“否”即该任务不得顺手启动对应真实环境。各档允许与不允许的证据如下，避免同一行被读成“离线通过即真实通过”。

| 档位 | 判据 | 允许的证据 | 不允许 |
| --- | --- | --- | --- |
| 离线Fake | 注入Fake模型/存储/时钟/传输，不启实例、不出站 | 契约、权限分支、失败与恢复时序、负例 | 冒充真实召回、真实模型质量、真实数据库约束或真实并发 |
| 真实PostgreSQL | 走`test_presales_postgres.py`临时回环实例并登记主版本/PGVector/角色 | 迁移、约束、并发与崩溃点、旧代次拒写 | SQLite替代、跳过计入通过、只跑离线SQL文本 |
| 真实MCP/HTTP | 启动任务13交付的stdio入口或售前ASGI（`PYTHONPATH=src`） | 进程寿命、凭证隔离、真实往返与退出 | 以Fake transport或Streamlit脚本冒充 |
| 真实模型 | 已复核专项计划+明确调用授权+记录模型与模式 | 结构输出、容量、四专家/Reflection、预算与费用 | 无Key静默跳过、失败后切Fake、用验收标签调优 |
| 真实浏览器 | 31锁定的浏览器版本+`--execute`显式opt-in | 页面恢复、终态交接、撤权与越权拒绝 | `typecheck`/`build`代替E2E、复用Streamlit证据 |

### 2.2 源码导入路径与本机启动命令

以下是拟新增入口交付后的PowerShell命令模板，只供任务14/32的受控验证环境使用，本次不执行。工作目录为仓库根目录。工具调用凭证、数据库连接和模型配置须由测试编排器向子进程注入，本模板不携带秘密；导入路径使用进程环境，不假设pytest的`pythonpath`对uvicorn或普通Python生效。

任务13交付的stdio入口（仅在任务14等真实进程验证时启动）：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
uv run python -m presales.infrastructure.opportunity_workspace
```

售前HTTP入口（由任务31编排器在任务32受控环境中启动）：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
uv run uvicorn presales.asgi:app --host 127.0.0.1 --port 8000
```

自动化编排应把源码路径和必要配置传入子进程环境，避免改写宿主会话的长期设置；若端口被占用，任务31为临时实例分配端口并传给客户端。MCP stdio由父进程管理stdin/stdout及生命周期，不以手工启动后闲置进程作为集成证据；Windows后台helper按项目规则隐藏窗口并有界回收。

## 3. 有序实施任务清单

### 3.1 2B-0：进入评审、契约与存储门禁

| 编号 | 任务名 | 所属增量 | 目标 | 涉及模块/文件 | 前置任务 | 交付物 | 验收条件 | 验证命令 | 不做什么 | 外部依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Proposal Analysis Attempt进入条件审查 | 2B-0 | 核对外部进入前置并登记二B内部交付门禁，使后续任务无需自我前置即可按授权推进 | 本清单；`STATUS.md`、二A/二B方案只读 | 无 | 进入评审记录、D0—D8证据索引及阻塞项 | AC19/24关联退出证据：D0的方案批准/实施授权可查，D1/D2/D3报告可核实，D4基础与现码相符；缺外部进入证据则相关实现阻塞；D5/D7/D8注明由5/6、25/26、31/32产出而非要求先完成；D6仅核对真实任务需要的环境要求；未取得证据不得记就绪，不新增重复签署要求 | 按项目约定填写：`git status --short`；`uv run pymarkdown scan docs/superpowers/plans/Phase-Two-B-Implementation-v1.md`；人工逐项核对，不启动服务 | 不补齐二A、不改STATUS、不将方案建议批准化 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置D0/D1/D2/D3，依赖【对应能力及评审完成后】解除阻塞 |
| 2 | Proposal Analysis Attempt身份与执行契约 | 2B-0 | 任务/Attempt/thread/修订/代次及命令错误契约可被确定性校验 | `src/presales/domain/contracts.py`；拟新增`src/presales/domain/proposal_analysis_attempt.py`与`tests/presales/test_proposal_attempt_contract.py` | 1；依赖【D0/D1/D2/D3完成后】 | Attempt、阶段/结局、命令/恢复/投影DTO，人工等待扩展契约 | AC19/13：伪造actor/thread/端点拒绝；业务状态不新增执行枚举；取消/撤权终态不可复活；同键异参报冲突；输出错误码可判定 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_contract.py -q` | 不连数据库，不实现审批interrupt，不新建术语 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置D0—D3及任务1 |
| 3 | Proposal Package Schema 1.0与Proposal Draft兼容 | 2B-0 | 完整方案与阶段一手工草稿能明确区分且不静默补齐 | 拟新增`src/presales/domain/proposal_package.py`；`src/presales/domain/contracts.py`；拟新增`tests/presales/test_proposal_package_schema.py` | 2 | P06全部区块类型/机器Schema、旧草稿适配、服务端身份字段规则 | AC07—10/21/27：必需区块/关联缺失拒绝；review/approval/external execution未执行明确；memory_updates仅候选；旧1.0草稿不能通过完整性校验；JSON可唯一生成DTO | 按项目约定填写：`uv run pytest tests/presales/test_proposal_package_schema.py -q` | 不造批准记录、不写记忆、不渲染完整导出、不评语义质量 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务2 |
| 4 | Proposal Analysis Attempt持久记录与最小权限 | 2B-0 | Attempt、两级占位、调用/恢复/投影及产物关系具备可迁移的约束和存储接口 | `migrations/versions/`拟新增`0005_proposal_attempt.py`（`down_revision=0004_knowledge`；最终文件名开工核对后记入交付记录，不改语义）；拟新增`src/presales/infrastructure/proposal_attempt_store.py`及`tests/presales/test_proposal_attempt_storage_contract.py` | 2、3；可与5并行，先固定共享代次接口 | 显式迁移、索引/唯一约束、受限状态更新权、短事务存储实现；约束离线测试 | AC19/13/16：任务/全局占位及一Attempt一thread唯一；投影修订同事务递增；候选/来源/终态写入有条件；运行/维护角色分离与“无任意授权写”只以迁移GRANT及SQL文本静态断言证明（离线档不证明真实角色权限）；离线SQL契约通过，真实约束与越权写入待6 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_storage_contract.py -q` | 不自动执行迁移、不启动实例、不以SQL断言代替数据库保证 | 真实模型否；真实数据库否（实跑独立任务6）；浏览器E2E否；未完成前置任务2/3 |
| 5 | Proposal Analysis Attempt受控checkpoint适配 | 2B-0 | 已锁定saver的checkpoint/pending writes提交路径能在事务内检查当前代次 | `uv.lock`只读；拟新增`src/presales/infrastructure/proposal_checkpoint.py`、`tests/presales/test_proposal_checkpoint_contract.py` | 2；可与4并行 | saver兼容记录、受控适配、消费回执与拒写契约，300—800行含测试 | AC19/13：确认锁定版本事务入口；checkpoint、pending writes、回执都受同提交边界校验；不支持时明确阻断续跑；不能用外层先读代次再调用原saver冒充fencing，真实竞态待6 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_checkpoint_contract.py -q` | 不放宽TD-033、不升级整个依赖树、不碰通用聊天checkpoint | 真实模型否；真实数据库否（实跑独立任务6）；浏览器E2E否；未完成前置任务2 |
| 6 | Proposal Analysis Attempt存储与checkpoint真实数据库门禁 | 2B-0 | 在临时PostgreSQL中证实迁移、权限和旧代次提交隔离，或明确阻塞恢复 | `scripts/test_presales_postgres.py`；拟新增`tests/presales/test_proposal_storage_postgres.py` | 4、5；依赖【D6真实临时实例就绪后】 | 专项测试、按用途分档的 pytest 子进程超时（单场景`-k`档与`--full-suite`全量档）、实例主版本/PGVector/角色登记、真实实例报告 | AC19/13/16：迁移可重复upgrade至head两次不破坏（测试内二次执行，不依赖脚本单次路径）；运行/维护角色真实隔离；旧owner暂停后在接管/取消之后提交checkpoint及pending writes均失败；回执同代次受限；不足则任务11恢复保持阻塞 | 按项目约定填写，需真实实例：`uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --pytest-args -q -rs -k proposal_storage_postgres` | 不用SQLite替代、不调用模型、不在默认离线套件偷偷启实例 | 真实模型否；真实数据库是；浏览器E2E否；未完成前置D6及任务4/5；D5的真实门禁证据由本任务产出，不作为自身前置 |

#### 2B-0续作范围与审查切片（2026-11-11）

用户已在本次续作指令中批准B层PostgreSQL命令，并要求连续完成2B-0验证；任务1的D0—D4进入证据以`docs/maintenance/phase2b-entry-closure-2026-11-11.md`及其引用工件为准。这里记录执行授权，不回写上游设计、STATUS或阶段验收状态。实际执行结果另见2B-0交付记录，不能继续把本文开头的历史“所有任务未执行”描述作为当前执行结论。

开工核对发现任务3已有单文件722行，加既有测试312行超过800行审查粒度。任务3保留原编号、依赖及业务范围，拆成两个内部审查切片：3a为`proposal_package_sections.py`中的来源、证据、需求、覆盖及枚举基础契约和对应专项测试；3b为`proposal_package.py`中的聚合区块、关联校验、机器Schema与旧草稿兼容及原专项测试。两切片分别控制在800行内，3b依赖3a；公共导入仍由`proposal_package.py`提供，不新增业务能力或第二份权威契约。

任务4迁移最终使用`0006_proposal_attempt.py`，`down_revision=0005_retrieval`，因为二A前置已经交付`0005_retrieval.py`；不得覆盖二A迁移。任务5锁定兼容对象为`uv.lock`中的LangGraph 1.2.11、checkpoint 4.2.0、checkpoint-postgres 3.1.2与psycopg 3.3.4。任务6补充旧执行者暂停/并发接管或取消/迟到提交、事务回滚及回执发布时机证据；不实现任务11的完整恢复业务。

任务4另按强耦合边界分为4a（Attempt/槽位迁移、状态存储及既有测试）和4b（调用/恢复/节点产物记录与checkpoint提交回执的迁移增补、`proposal_execution_records.py`及专项测试）。共同使用0006迁移；4b只交付持久化约束和接口，不提前实现任务7/11/15的调度、恢复决策或来源服务。checkpoint提交回执由任务5在与checkpoint相同事务中写入；恢复意图的业务消费判定仍由任务11实现。

### 3.2 2B-1：Proposal Analysis Attempt后台生命周期

| 编号 | 任务名 | 所属增量 | 目标 | 涉及模块/文件 | 前置任务 | 交付物 | 验收条件 | 验证命令 | 不做什么 | 外部依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 7 | Proposal Analysis Attempt授权与调用预算 | 2B-1 | 每个受限边界能拒绝失效上下文并持久累计调用次数和主动预算 | 拟新增`src/presales/application/proposal_attempt_context.py`；`src/presales/infrastructure/outbound.py`及拟新增同模块`proposal_model.py`；拟新增`tests/presales/test_proposal_attempt_context.py` | 2、4、6 | 当前授权交集门禁、显式Fake/真实兼容API或本地LLM客户端适配、预算/时钟接口；客户端用Fake HTTP验证，真实调用仅26 | AC04/05/19/22：启动范围不可扩；读取/发起/接收/落库均重验；无配置不发请求；120秒/10秒/10分钟与初次后最多2次计数有Fake时钟边界；重启不归零、未知区间保守计入；观测无正文秘密；真实失败不切Fake | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_context.py -q` | 不读真实凭据、不发真实请求、不把读取权当外发批准 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务6 |
| 8 | Presales Task启动Proposal Analysis Attempt | 2B-1 | 一次合法命令原子建立Attempt、占位、thread、任务状态和回执 | 拟新增`src/presales/application/proposal_attempt_commands.py`；`src/presales/infrastructure/proposal_attempt_store.py`；拟新增`tests/presales/test_proposal_attempt_start.py` | 4、6、7 | 启动/同请求核实服务、输入锁与背景必需性锁定 | AC19/13：合法状态启动；全局忙不建排队Attempt；关键材料/未确认背景/超限拒绝；启动前容量与模式预检（关键输入装不下即拒绝，不静默截断；未显式选择模型模式不启动）；同键同参复用、异参拒绝；输入与候选修订检查，真实并发待12 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_start.py -q` | 不开放HTTP、不自动重跑旧任务、不以进程锁作为唯一排他 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务6/7 |
| 9 | Proposal Analysis Attempt监督器与售前入口寿命 | 2B-1 | 已受理Attempt可被单一执行者领取且不依赖请求或SSE存活 | 拟新增`src/presales/application/proposal_attempt_runtime.py`；`src/presales/api/app.py`（同API模块内按需接入`auth.py` lifespan）；拟新增`tests/presales/test_proposal_attempt_runtime.py` | 5、6、8 | 监督器领取/心跳/关闭、售前生命周期装配，图执行使用注入端口 | AC19/24：请求返回/关页/退出不取消；受理未派发可核对；重启只核实不外发；数据库失联停止新调用；心跳超时不直接接管；关闭超限保留未知，不启动通用research图 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_runtime.py -q` | 不引队列/高可用服务、不把新启动记录当自动恢复授权 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务8 |
| 10 | Presales Task取消与Proposal Analysis Attempt排空 | 2B-1 | 取消持久失效旧执行且未核实在途结果前不释放全局槽位 | `src/presales/application/proposal_attempt_commands.py`、`proposal_attempt_runtime.py`；拟新增`tests/presales/test_proposal_attempt_cancel.py` | 7、8、9 | 幂等取消、停止调度、排空/隔离及受限结果处理 | AC19/13：取消先/落稿先两序可判定；批准后拒绝取消；迟到结果不可写候选/旧checkpoint；仅断HTTP不算取消；未知在途继续占位；已取消任务不可复活，真实竞争待12/24 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_cancel.py -q` | 不承诺供应商撤回/退款，不提供只取消Attempt后复活任务的新业务操作 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务9 |
| 11 | Proposal Analysis Attempt重启核实与受控继续 | 2B-1 | 原发起人能在核实通过后续跑原Attempt且重复恢复不重复消费 | `src/presales/application/proposal_attempt_runtime.py`；拟新增`src/presales/application/proposal_attempt_recovery.py`、`tests/presales/test_proposal_attempt_recovery.py` | 5、6、7、9、10 | 扫描/对账、稳定恢复意图及消费回执、占位重取 | AC19/13：重新认证、原actor/输入/候选/来源/预算重验；忙则保留等待不排队；未知不盲重试；终态不可复活；业务已落稿只对账；人工等待扩展顺序契约可测但不接审批 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_recovery.py -q` | 不实现审批resume，不把未发现标记当未执行，不放宽saver门禁 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务6/9/10 |
| 12 | Proposal Analysis Attempt生命周期真实并发验证 | 2B-1 | 多进程/崩溃点下仍满足1/1占位、预算和旧执行隔离 | 拟新增`tests/presales/test_proposal_lifecycle_postgres.py`；现有临时PostgreSQL脚本 | 6、8、9、10、11；依赖【D6就绪后】 | 可复现并发/崩溃注入场景（必须是真实进程终止、真实连接中断或真实事务竞争）、数据库专项报告 | AC19/13/16：双启动一次受理；提交到派发间崩溃可核实；超时旧owner回归拒写；取消与接管不提前释放；重启保留预算/修订次数；撤权恢复禁止；checkpoint缺损保持待核实；不重复6的单事务契约结论，只验证并发/崩溃时序；禁止以异常注入或单进程模拟替代真实崩溃 | 按项目约定填写，需真实实例：`uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --pytest-args -q -rs -k proposal_lifecycle_postgres` | 不调用真实模型、不冒称完整四专家链或浏览器验证 | 真实模型否；真实数据库是；浏览器E2E否；未完成前置任务6/8—11 |

### 3.3 2B-2：Evidence、专业分析、Proposal Draft与探针

| 编号 | 任务名 | 所属增量 | 目标 | 涉及模块/文件 | 前置任务 | 交付物 | 验收条件 | 验证命令 | 不做什么 | 外部依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 13 | Opportunity只读背景与可信来源适配 | 2B-2 | 背景必需性、受限读取及来源契约在Fake传输下可判定 | 拟新增`src/presales/application/opportunity_context.py`、`src/presales/infrastructure/opportunity_workspace.py`及`tests/presales/test_opportunity_context.py` | 3、7、12；依赖【D1共享MCP/授权接口完成后】 | 只读业务适配、后台可信调用封装、stdio服务可执行入口；交付位置为上列opportunity_workspace.py，启动完整命令见§2.2；默认路径如因实现核对改变须同步修订命令 | AC04/05/09/19/22：必需失败停止、可选失败明确缺失、未确认拒绝；成功空历史不同于失败；无权与不存在不区分、不泄漏（六类结局逐类）；任务授权不向上；来源ID/版本可解析；无actor信任/写工具/地址注入 | 按项目约定填写：`uv run pytest tests/presales/test_opportunity_context.py -q` | 不启动子进程/真实数据库、不接CRM、不注册External Write | 真实模型否；真实数据库否；浏览器E2E否；未完成前置D1及任务12 |
| 14 | Opportunity只读MCP进程与授权真实集成 | 2B-2 | 本机stdio进程真实往返、退出和共享来源授权经专项验证 | 拟新增`tests/presales/test_opportunity_mcp_process.py`；现有临时PostgreSQL脚本 | 6、12、13；依赖【D1/D6及本机MCP运行时完成后】 | Windows进程启动/回收、短期凭证隔离及真实API/MCP等权范围报告 | AC04/05/19/22：伪造主体/Attempt拒绝；撤权/超时/EOF/子进程异常可判定；秘密不入Prompt/checkpoint；直接写调用拒绝；Mock背景来源落同一登记库而非独立ACL | 按项目约定填写，需真实实例及MCP子进程：`uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --pytest-args -q -rs -k opportunity_mcp_process` | 不把Fake transport通过当stdio通过，不连外部CRM/真实模型 | 真实模型否；真实数据库是；浏览器E2E否；本机真实MCP服务是（＝任务13交付的stdio入口，不等同D1的二A Product Catalog MCP运行时）；未完成前置D1/D6/13 |
| 15 | Evidence Snapshot与Derived Artifact来源闭包 | 2B-2 | 实际读取和中间产物形成完整依赖，未就绪内容无法读取 | 拟新增`src/presales/application/evidence_snapshot.py`；`src/presales/access/policy.py`；拟新增`tests/presales/test_evidence_snapshot.py` | 3、4、7、13；可与14并行，真实验证待24 | 来源登记/稳定节点执行身份、待就绪/对账服务 | AC04/05/09/13/16：引用删除不删实际依赖；材料/MCP/检索/Reflection输入均登记；环/缺来源拒绝；同节点结果复用；停用/撤权和旧代次不能激活产物 | 按项目约定填写：`uv run pytest tests/presales/test_evidence_snapshot.py -q` | 不私建检索目录、不把来源可定位等同论断受支持 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务13 |
| 16 | Requirement与Proposal Analysis Attempt白名单计划 | 2B-2 | Router产生有界计划并保留需求身份、出处及客户范围 | 拟新增`src/presales/application/requirement_analysis.py`；拟新增`tests/presales/test_requirement_analysis.py` | 3、7、15 | 意图/Router/需求节点、结构化提示模板及Fake案例 | AC07/08/09/19：不能删关键专家/开无限循环；拆合保留原始出处，未经确认不改分母；关键输入装不下明确拒绝；稳定ID服务端产生 | 按项目约定填写：`uv run pytest tests/presales/test_requirement_analysis.py -q` | 不用真实模型评分、不替客户确认范围、不并行读取尚不存在的上游结果 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务15 |
| 17 | Evidence与Published Knowledge Version专业分析 | 2B-2 | 需求驱动证据节点只使用允许范围内的已发布知识与独立任务材料 | 拟新增`src/presales/application/evidence_analysis.py`、`tests/presales/test_evidence_analysis.py` | 16；依赖【D1受限检索及Product Catalog MCP完成后】 | 二A检索/目录业务适配、引用/无证据/冲突及降级记录 | AC04/05/06/07/09/16/19：两路和缓存范围由二A接口保证，Fake记录证明不越权调用；失败与空结果区分；精排超时可见退RRF；证据缺失不虚构、版本/适用性保留，真实链待24 | 按项目约定填写：`uv run pytest tests/presales/test_evidence_analysis.py -q` | 不重写BM25/向量库、不把候选知识当已发布、不以Fake证明召回率 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置D1/16 |
| 18 | Proposal Package方案架构分析 | 2B-2 | 架构节点在需求和证据之后输出可校验的部署/集成建议及条件 | 拟新增`src/presales/application/proposal_solution_analysis.py`、`tests/presales/test_proposal_solution_analysis.py` | 16、17 | 架构节点、方案/假设/限制类型化输出与Fake负例 | AC07/09/19：不支持项不虚构替代承诺；事实/建议/待确认区分；报价/日期非绑定；关键结构缺失失败；预算不足不截断成完整 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_solution_analysis.py -q` | 不生成正式合同/报价，不承诺真实模型诚实性已达标 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务17 |
| 19 | Risk与Clarification Question专业分析 | 2B-2 | 风险节点依赖架构和证据，形成六类风险及待澄清的完整检查结果 | 拟新增`src/presales/application/proposal_risk_analysis.py`、`tests/presales/test_proposal_risk_analysis.py` | 18 | 风险/问题节点、空列表检查记录、处置待办 | AC09/10/19：严重风险不豁免；无来源回复不解决关键问题；无风险可为空但检查完成；缺关键产出失败；不伪造责任人接受/客户确认 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_risk_analysis.py -q` | 不提供人工改级/录入入口、不写Review Record或Approval Record | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务18 |
| 20 | Product-fit Coverage与Proposal Package汇总门禁 | 2B-2 | 汇总确定计算覆盖并输出可核对的阻断清单 | 拟新增`src/presales/application/proposal_package_validation.py`、`tests/presales/test_proposal_package_validation.py` | 3、16、17、18、19 | 四专家完整性/引用关联/覆盖与风险校验器、前版汇总 | AC07—10/27：10条6覆盖得60%；部分不折算、范围外入分母；空范围不适用；必须未覆盖/待澄清/范围外阻断，部分/高风险无真实接受不得解锁；冲突/缺引用拒绝完整，内外指标不混算 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_package_validation.py -q` | 不实现正式提交/批准；不因结构校验通过宣称语义正确 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务19 |
| 21 | Proposal Package一次Reflection与前后关联 | 2B-2 | 一次受约束修订保留两版并拒绝硬门禁退化 | 拟新增`src/presales/application/proposal_reflection.py`；`proposal_package_validation.py`；拟新增`tests/presales/test_proposal_reflection.py` | 7、15、20 | 检查/修订身份、一次配额、前后产物/差异和失败原因 | AC21/27及AC07—10：技术重试/重启不增修订数；有效无修改仍有检查记录；缺配对失败；不得删分母/无依据降风险/伪造接受；退化后版隔离，前版不自动冒充成功 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_reflection.py -q` | 不自改提示词/代码、不无限修订、不以Judge取代正式人工配对评分 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务20 |
| 22 | Proposal Draft分析落稿与失败保留 | 2B-2 | 专门落稿事务只让当前有效Attempt推进候选并保留失败历史 | 拟新增`src/presales/application/proposal_draft_finalization.py`；`src/presales/infrastructure/proposal_attempt_store.py`；拟新增`tests/presales/test_proposal_draft_finalization.py` | 10、11、15、20、21 | 内容/来源/Evidence Snapshot/候选/Attempt终态/投影/审计原子落稿 | AC04/05/07—10/13/16/19/21：草稿就绪不等于允许提交；合法不支持可成为草稿但保留门禁；缺专家/Reflection失败不覆盖旧草稿；落稿事务内重验 actor 当前角色/对象授权与全部实际来源闭包，撤权或来源失效即拒写；取消旧代次拒写；落稿成功checkpoint滞后仅对账；真实原子性待24 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_draft_finalization.py -q` | 不绕过`_task(new)`直接复用旧保存入口、不开放任意状态写、不写正式版本 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务21 |
| 23 | Proposal Analysis Attempt显式图装配 | 2B-2 | 监督器可驱动白名单DAG至受限草稿，重入不重复产生权威产物 | 拟新增`src/presales/application/proposal_analysis_graph.py`；`proposal_attempt_runtime.py`；拟新增`tests/presales/test_proposal_analysis_graph.py` | 9、11、13、15—22 | StateGraph、受控模型/工具接线与确定性全图工程案例 | AC07—10/19/21/27：需求→证据→架构→风险→汇总→Reflection→校验→落稿顺序固定；四类缺一失败；已提交节点复用；无审批/写工具；全链无Key可运行且模式显式 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_analysis_graph.py -q` | 不加载通用research/RAG/AG-UI图、不提前接人工审批子流程 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务22 |
| 24 | Proposal Analysis Attempt与Evidence Snapshot真实集成回归 | 2B-2 | 完整Fake分析图在真实数据库/二A检索/只读工具下通过权限与崩溃点门禁 | 拟新增`tests/presales/test_proposal_analysis_postgres.py`；现有专项脚本与已交付测试 | 12、14、17、22、23；依赖【D1/D3/D6完成后】 | 全图数据库专项、共享模块离线回归及基线失败分类报告 | AC04—10/13/16/19/21/22/27：真实撤权、目录变化、来源闭包、取消落稿竞争、产物已提交而checkpoint滞后无二次候选；Fake不外发；指定工程反例全有非跳过证据；既有失败单列不虚报全套通过 | 按项目约定填写，需真实实例：`uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --full-suite --pytest-args -q -rs`（该命令走全量档，须使用任务6核定的`--full-suite`档超时；未分档前只作目标命令，不计已运行）；另`uv run pytest`、`uv run pyrefly check`（离线模式单列） | 不把真实数据库+Fake当真实Agent能力，不执行阶段三/四/五完整验收 | 真实模型否；真实数据库是；浏览器E2E否；真实本地只读MCP是；未完成前置D1/D3/D6及任务23 |
| 25 | Golden Dataset开发样例与Agent Capability Evaluation探针准备 | 2B-2 | 少量开发输入、检查项和有界执行入口已固定且默认不调用真实服务 | 拟新增`scripts/probe_proposal_analysis.py`、`tests/presales/test_proposal_probe_runner.py`；拟新增`data/probes/phase2b-dev/`存放所选合成材料并登记校验和，不放客户数据；已存在开发样例则复用，尚未制作则准备少量映射到批准的10开发样例组成并送复核 | 3、23、24；样例准备/送复核是本任务交付，不是本任务前置 | 专项计划草案、少量开发样例/人工预期及复核记录或明确待复核清单、报告格式和默认dry-run入口；runner与资料两逻辑模块，超800行先修订拆分 | AC19/21/27及AC20准备部分：所选样例/预期/检查项及版本可追溯且不读取20验收标签；默认及CLI离线自测无外发；未具备数据复核、固定专项计划或明确调用授权则execute-real必须拒绝；报告保留超时/缺引用/缺配对、三类结局与未知费用；允许形成准备交付并标待复核，但该结局不解除26执行门禁 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_probe_runner.py -q`；新CLI交付后`uv run python scripts/probe_proposal_analysis.py --help` | 不执行真实探针、不制作或批准全部冻结验收集、不改变P07阈值；开发样例可准备/送复核，不能自称已批准 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务24；D7中的数据复核/真实调用授权不阻塞本任务准备，转为26的执行前置 |
| 26 | Agent Capability Evaluation早期真实探针执行 | 2B-2 | 经授权真实运行留下全部产物并给出可行性/差距/阻断结论 | 任务25交付的探针脚本、专项计划与受限报告；存储产物由既有业务服务生成 | 14、24、25；依赖【D6就绪、所选开发数据复核、专项计划固定及本次真实调用明确授权后】 | 独立探针报告、Attempt/前后产物/配置/时间/可得用量费用、差例和影响清单 | AC07/09/10/19/21/27及AC20准备部分：实际结构输出/容量/四专家/Reflection/预算均检查；失败不换Fake/不删样本；按上游规则归类并记录依据与影响任务；基本不可行先解决，语义差距留痕可继续已授权工程，越权等硬门禁立即阻断且不等额外签署；结论有争议或拟改规则/授权范围时提请裁决，未解决的争议不得当作可继续；不宣称冻结验收通过 | 按项目约定填写，需真实实例及已授权模型，新入口交付后：`uv run python scripts/probe_proposal_analysis.py --plan "<已复核专项计划JSON>" --execute-real` | 不进入默认CI、不跑20验收标签调优、不扩调用次数/数据范围，不等待运行页全部完成才探针 | 真实模型是（含配置的真实本地LLM）；真实数据库是；浏览器E2E否；本地MCP是；未完成前置D6/D7及任务25；未授权则阻塞 |

### 3.4 2B-3：REST、SSE与运行页

| 编号 | 任务名 | 所属增量 | 目标 | 涉及模块/文件 | 前置任务 | 交付物 | 验收条件 | 验证命令 | 不做什么 | 外部依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 27 | Presales Task与Proposal Analysis Attempt命令API | 2B-3 | 启动/取消/continue/命令核实通过业务JWT和同一应用门禁响应 | 拟新增`src/presales/api/proposal_attempts.py`；`src/presales/api/app.py`；拟新增`tests/presales/test_proposal_attempt_api.py` | 8、10、11、24、26（条件：报告可核实且无未解决的基本不可行或硬门禁失败影响本任务）；在25、26之后执行，不与25并行 | REST DTO/OpenAPI、202受理/冲突/忙/未就绪错误及鉴权反例 | AC04/05/13/19/22：不接受actor/grant/thread/端点；丢响应同键核实；取消和continue分别校验；未知对象/无权统一不泄漏；无通用AG-UI旁路；Fake服务下契约通过，真实验证待32 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_api.py -q` | 不提供批准/发布命令、不通过HTTP偷偷恢复未知执行 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务24/26及D2业务API集成；语义差距留痕后按既有授权推进，不新增放行签署 |
| 28 | Proposal Analysis Attempt受限快照与SSE | 2B-3 | 投影一致性读取及持续验权通知可避免漏终态且不暴露graph state | 拟新增`src/presales/application/proposal_attempt_queries.py`、`src/presales/api/proposal_attempt_events.py`及`tests/presales/test_proposal_attempt_events.py` | 7、15、24、26（条件：无未解决的基本不可行或硬门禁失败影响本任务）、27 | 快照/历史产物查询、ready/changed/terminal/heartbeat与持续鉴权 | AC04/05/13/16/19：同事务修订；订阅前/ready间/读快照后终态均可重查；EOF非成功；不完整就绪不露正文；token到期/读者撤权终止，发起人撤权另行终止运行；无Prompt/秘密/原始token，真实终态待32 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_attempt_events.py -q` | 不承诺全事件回放、不以独立sequence作提交顺序、不做审批恢复 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置任务27 |
| 29 | Proposal Analysis Attempt前端契约与订阅状态 | 2B-3 | 前端从唯一后端定义取得DTO并按修订同步快照及事件 | `frontend/src/api.ts`；拟新增`frontend/src/proposalAnalysisAttempt.ts`、`frontend/src/generated/`；拟新增`scripts/export_proposal_contract.py`（由任务2/3的Pydantic模型导出JSON Schema及TS类型，导出命令在交付时记入修订记录） | 2、3、27、28；26条件经27/28传递，不能与26尚未完成的必要报告整理并行验收 | DTO生成流程、Bearer fetch流客户端、缓冲/去重/兜底重查/清缓存 | AC19/13/16：旧Attempt事件不污染新运行；乱序/溢出转快照；到期退出清缓存；JWT不入URL/持久存储；无自动重启/continue；typecheck/build证明工程一致性，交互竞态必须待32 | 按项目约定填写，`frontend/`下：`npm run typecheck`；`npm run build`；DTO导出命令在本任务交付时记录，不捏造现成生成脚本 | 不添加Node业务服务、不手写第二套权威状态枚举、不以构建代替浏览器验收 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置D2及任务28 |
| 30 | Presales Task运行页与Proposal Draft/Evidence比较 | 2B-3 | 页面展示运行/排空/就绪/门禁与受限草稿证据，并只提交显式合法操作 | `frontend/src/main.tsx`、`styles.css`；拟新增`frontend/src/ProposalAnalysisAttemptPage.tsx`（同前端运行页模块） | 26（条件：报告可核实且无未解决的基本不可行或硬门禁失败影响本任务）、29；D2完成 | 运行页、历史Attempt、节点/模式/错误/预算、证据定位与Reflection差异 | AC07—10/19/21/27：取消告知旧任务不可复活；刷新先登录读状态；终态与结果未就绪分别显示；失败/取消显著标记；上传/模型文本不执行脚本；无编辑/风险接受/审批按钮；交互实证待32 | 按项目约定填写，`frontend/`下：`npm run typecheck`；`npm run build` | 不做完整编辑器、客户确认、审批发布页，不把语义探针不足伪装验收通过 | 真实模型否；真实数据库否；浏览器E2E否；未完成前置D2/26/29；26未执行或其基本不可行/硬门禁影响本任务则阻塞，语义差距登记后按已有授权继续 |
| 31 | Proposal Analysis Attempt浏览器验证入口准备 | 2B-3 | React专用场景可枚举且默认不启动真实环境，显式执行才创建临时环境 | 拟新增`scripts/test_presales_browser.py`、`tests/presales/test_proposal_browser_harness.py`；复用`test_presales_postgres.py`方案，必要共享适配同scripts模块；将`playwright`及浏览器版本锁定写入`pyproject.toml`（或统一锁定文件），供32引用 | 27、28、29、30 | 受控临时PG/售前API/静态入口/Fake模型/只读MCP编排、锁定的浏览器环境要求、与32逐项对应的场景清单及CLI离线自测；D8入口由本任务交付 | AC19/24：默认list/help无实例/浏览器/外发；真实执行明确opt-in；子进程使用§2.2的源码路径及presales.asgi:app，测试凭据临时生成且隐藏；Windows helper隐藏运行并有界回收；32场景逐项可枚举；离线构造启动参数/配置可测，不要求真实浏览器或运行服务先存在；真实执行前缺环境须报阻塞 | 按项目约定填写：`uv run pytest tests/presales/test_proposal_browser_harness.py -q`；新脚本交付后`uv run python scripts/test_presales_browser.py --list` | 本任务不启动真实浏览器/数据库，不把Streamlit现有脚本当React验证，不安装依赖以冒充执行成果 | 真实模型否；真实数据库否；浏览器E2E否（只准备）；未完成前置任务30；D8入口和浏览器环境要求由本任务准备，真实浏览器/HTTP/PG可用性只阻塞32，不自阻塞31 |
| 32 | Proposal Analysis Attempt真实浏览器与HTTP联调 | 2B-3 | 真实React/售前HTTP/数据库联调证明页面恢复、终态和权限行为 | 任务31脚本及场景；`frontend/`构建产物；受限浏览器报告（路径开工确认） | 12、14、24、26、28、30、31；依赖【D2/D6、31交付的D8入口及本机真实浏览器就绪后】 | 原生浏览器E2E报告、HTTP/数据库断言、必要截图/失败记录 | AC04/05/07—10/13/16/19/21/27：关页继续、刷新/重登不重复启动；取消/恢复/忙；终态各交接窗口、丢乱序/EOF/缓冲溢出；到期/读者及发起人撤权；旧缓存/产物越权拒绝；脚本文本不执行；草稿/证据/前后比较一致 | 按项目约定填写，需真实实例/Chromium，31交付后：`uv run --with "playwright==<31写入的锁定版本>" python scripts/test_presales_browser.py --pg-bin "<PostgreSQL bin>" --execute`；playwright及浏览器版本一律取自31的锁定值，不得浮动或临时拉取；缺浏览器先报告环境阻塞 | 不调用真实模型（显式Fake），不拿typecheck替代E2E，不覆盖Linux Compose交付 | 真实模型否；真实数据库是；浏览器E2E是；真实本地HTTP/MCP是；未完成前置D2/D6/D8及任务31 |

### 3.5 二B退出评审

| 编号 | 任务名 | 所属增量 | 目标 | 涉及模块/文件 | 前置任务 | 交付物 | 验收条件 | 验证命令 | 不做什么 | 外部依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 33 | Proposal Analysis Attempt证据汇总与退出评审 | 二B退出评审 | 每项交付有独立结论且所有缺口映射后续处理，提交可审查退出材料 | `docs/maintenance/phase2b-delivery-<YYYY-MM-DD>.md`（拟新增；最终文件名开工核对后记入修订记录）；本清单修订记录 | 1—32；任何阻塞未解除仅能形成未完成报告 | 工程/PG/MCP/真实探针/浏览器分册索引、AC部分覆盖、遗留风险和退出建议 | AC07—10/19/21/27：每个映射项都有任务与证据，失败/跳过/未授权分别记录；环境身份（PG主版本/PGVector/角色、模型与模式、浏览器版本）随分册登记；二A/三/四/五缺口不伪称通过；探针语义差距可以带记录建议继续，但基本不可行/硬门禁失败及缺真实报告不能列已完成；用户决定阶段验收 | 按项目约定填写：`uv run pymarkdown scan docs/superpowers/plans/Phase-Two-B-Implementation-v1.md docs/maintenance/`；人工核对报告；不重新运行工程或真实服务命令 | 不改STATUS/需求/设计批准结论、不commit/push、不自行宣布验收 | 真实模型否；真实数据库否；浏览器E2E否；依赖任务6/12/14/24/26/32的真实报告，报告未取得则阻塞退出完成结论 |

## 4. 增量覆盖表

| 增量编号 | 承接任务编号 | 对原方案退出证据的覆盖 |
| --- | --- | --- |
| 2B-0 | 1、2、3、4、5、6 | 二A/评审依赖、旧草稿兼容、契约/迁移/最小权限、受控saver真实门禁；不能凭离线任务跳过6 |
| 2B-1 | 7、8、9、10、11、12 | 授权/预算、启动/占位、监督器、取消排空、核实恢复；12验证真实并发/崩溃 |
| 2B-2 | 13—26 | 只读Opportunity背景、来源快照、四专家、汇总/Reflection/落稿、全图联调；25准备/26真实探针分别验收 |
| 2B-2链路可用后早期探针（复用视图，主归属仍为2B-2） | 25、26（不重复计数） | 固定开发样例及调用边界、全部真实失败/用量报告、三类结果与推进约束；不是新增阶段，不改变25/26的主归属 |
| 2B-3 | 27、28、29、30、31、32 | REST、快照/SSE、DTO/订阅、运行页；31准备与32真实浏览器取证分离 |
| 二B退出评审 | 33 | 分册证据、未执行/失败/缺口、评审建议；不改变批准或阶段状态 |

## 5. AC覆盖表与证据映射

表内“证据”均为未来待取得，不是本次通过结果。同一AC有工程、真实探针、正式验收多个层次，二B只认领上游分配范围。

| AC编号 | 承接任务编号 | 检查项与对应证据 | 本清单覆盖边界 |
| --- | --- | --- | --- |
| AC07 | 3、16、17、18、20、21、22、23、24、26、30、32 | 不支持必须项仍入分母、保持阻断、无虚构替代承诺：Schema/计算负例、全图Fake、PG报告、真实探针问题记录及页面显示 | 真实语义探针先发现问题；正式批准拒绝在三复验，完整能力阈值在五 |
| AC08 | 3、16、20、23、24、30、32 | 10条6全覆盖=60%，2部分/1不明/1范围外分列；范围空不适用；产品覆盖与Agent质量分开：固定fixture和结果JSON/页面证据 | 不对Fake输出计算真实Agent成绩 |
| AC09 | 13、15、16、17、18、19、20、24、26、30、32 | 冲突双方版本/适用范围保留；缺证据标不足；无来源回答不改关键结论；旧快照不重写：节点、依赖、PG及浏览器报告 | 三实现人工澄清/冲突处置写入口，五完成识别质量验收 |
| AC10 | 3、19、20、21、22、24、26、30、32 | 严重不可豁免、高风险/部分必须缺真实接受不得解除：门禁JSON、负例、DB身份约束与页面 | 二B无批准入口；正式批准及防伪造决定在三 |
| AC19 | 1—14、16—19、22—33 | 模型异常/无配置/取消/全局忙/输入与预算超限、失败保留、无无限重试/Fake切换/截断；调用/占位/恢复/HTTP与页面结局：分层命令和非跳过报告 | 默认工程无Key，真实模型/数据库/浏览器分别取证；15只承载来源闭包，不承接AC19，其失败保留由15自身验收与24覆盖 |
| AC21 | 3、7、11、20、21、22、23、24、25、26、30、32 | 前后产物/变更及同一基线可比较；修订至多一次，重启/技术重试不放大；硬门禁退化隔离：离线计数、PG及真实探针/页面报告 | 平均质量差与完整人工四维配对正式验收留五 |
| AC27 | 3、20、21、23、24、25、26、30、32、33 | 超时/无输出/结构不可用全部适用指标计失败的fixture；黄金要求证据而无引用失败；缺配对失败阻止正式轮次通过；报告不删差例、不择优 | 25/26检查留证机制与开发样例，冻结样例适用集合和三轮分数仍在五 |
| AC04、AC05、AC22（二B部分） | 2、7、13、14、15、17、24、27、28、29、32 | 伪造身份/商机越权、派生来源限制、MCP及外发/注入拒绝；真实链与浏览器反例 | 不认领二A两路检索实现或完整跨阶段外发验收 |
| AC06、AC16（二B部分） | 7、11、15、17、24、28、32 | 二A精排降级事件传播；停用/撤权后新读取受限、旧结果不可继续回写 | 检索算法本身归二A；提交/批准证据复核与历史影响治理归三 |
| AC13（二B部分） | 2、4—6、8、10—12、15、22—24、27、28、32 | 旧代次/旧Attempt/旧修订拒写当前候选、恢复不重复消费：真实竞态与页面证据 | 审批决定失效和外部副作用竞态归三/四 |
| AC20、AC24（准备/运行边界部分） | 1、9、25、26、31、32、33 | 默认测试无真实费、真实探针显式身份/数据/预算、启动与报告可核查 | 不等于AC20冻结验收或AC24完整演示交付通过 |

### 5.1 原方案额外检查项逐行追踪

| 原方案检查项 | 承接任务编号 | 独立退出证据 |
| --- | --- | --- |
| 并发双启动/全局忙、同键异参、回执丢失 | 2、4、8、12、27、32 | 原子占位/请求回执真实竞争；浏览器响应丢失后仅核实 |
| 受理后未派发、调用发出后崩溃、已落稿checkpoint滞后、checkpoint损坏/旧pending writes | 5、6、9、11、12、15、22、24 | saver提交边界与恢复对账报告；缺产物不证明未执行 |
| 旧owner恢复、取消与落稿两顺序、撤权/迟到、排空前不释槽 | 6、7、10、11、12、22、24 | PostgreSQL竞态和受限调用记录；取消/排空分列 |
| 续跑预算/重试/thread/Reflection身份不重置，授权恢复/取消不复活 | 2、7、10、11、12、21、24 | Fake时钟边界与真实恢复回执；新Attempt/新任务身份明确 |
| SSE订阅前/ready间/快照后终态、事件丢乱序/溢出/代理断线、EOF非成功 | 28、29、31、32 | 后端通知契约及真实浏览器各竞争窗口重查快照 |
| token到期、读者/发起人撤权、来源扩大、缓存/历史/已知ID越权 | 7、15、24、27、28、29、32 | 后台与订阅不同身份语义，DB/HTTP/浏览器拒绝与缓存清理 |
| 必需/可选/未确认背景、空历史、仅任务权限、缺来源、注入/写工具 | 8、13、14、23、24、32 | MCP/业务API同权范围，直接写调用拒绝、失败与空区分 |
| 四专家缺产出、空风险有检查、引用/分母失配、风险无据降级、缺配对、人工补救非自动通过 | 3、16—23、24、25、26、30、32 | Schema/门禁/图负例与真实探针全量报告，不静默造接受 |
| D0—D8、业务入口/PGVector、saver/权限/跨平台、Schema旧形态/DTO | 1—6、9、12、14、24、27、29、31、32、33 | 依赖证据表、真实PG/本机进程/浏览器报告；Linux交付另记缺口 |
| 探针固定开发数据、结构/容量/四专家/Reflection/预算、三类结局与报告 | 1、25、26、33 | 审核后的专项计划、调用授权、全量差例及依赖影响清单 |

## 6. 缺口表

| 未覆盖或依赖外部条件的项 | 原因/当前状态 | 建议处理时机与影响 |
| --- | --- | --- |
| `_docs/superpowers/plans`指定输出路径 | 不存在且用户禁止建目录；本次采用已有docs路径 | 当前仅交付单个计划；后续获允许建目录或用户自行建立后再迁移，不在本轮创建 |
| 二B方案批准及实施授权 | 本清单v1.2已通过AI技术复评，但上游二B仍待评审，用户实施范围尚未授权；文档已评审不等于业务方案批准或允许开工 | 实施前完成任务1的D0核对，缺上游批准/相应实施授权仍不执行2起；不因本清单状态更新自动解除D1—D8 |
| 二A-2/2A-3及二A真实探针 | 无对应检索/MCP/业务页面或可用真实探针证据 | 二A专项先完成；依赖任务阻塞，不在本清单顺手补实现；语义差距按报告分类，不必假装达到阶段五阈值 |
| 真实PostgreSQL主版本/PGVector/权限可用性 | 当前源码有约束，未核实机器可用 | 6前核定专用临时实例，6/12/14/24/32分层取证；不连接未授权业务库 |
| saver提交边界fencing支持 | 通用saver存在不代表TD-033成立 | 5设计和6实证；无法拒旧代次则11续跑阻塞，不能用禁测/SQLite降级制造通过；若确认锁定版本无法在提交边界fencing，是否改为“一律结束旧Attempt、放弃受控续跑”须用户重定（第7节） |
| Golden Dataset开发样例与真实模型授权 | 批准样本组成不等于已有数据/服务许可 | 25负责少量选例准备和送复核，不能要求这些成果先存在；26在数据复核、计划固定及调用获授权后执行；未执行探针保持未验证 |
| 样例数/模型/端点/调用次数/费用上限 | 上游明确留专项计划，不能由本清单冒充用户选择 | 25给出有依据建议并固定输入，26执行前核对明确授权；不更改P07阈值 |
| 浏览器/本地MCP/HTTP基础设施 | Streamlit脚本不能证明React；新CLI尚不存在 | 13交付只读MCP及§2.2完整启动命令，14真实进程验证；31准备入口/锁定环境要求，不以自身成果或真实浏览器就绪为前置；32才核实真实环境并执行，未满足不以build替代 |
| 阶段三编辑、客户澄清/冲突确认、Review/Approval与人工interrupt | 二B正常路径结束在草稿，无正式批准入口 | 三实现业务写交互/批准和等待释放重取全链；11只保留基础契约，AC09/10/13不冒称完整覆盖 |
| 阶段四External Task Record、Approved Action Set、发布/未知写恢复 | 二B只读背景，禁止未经批准External Write | 四实现；14/24只验证写能力不可调用，不实现写幂等协议 |
| Long-term Memory治理/确认 | 二B仅memory_updates候选，待确认不变事实 | 三治理与AC17；不得以方案生成自动晋升事实 |
| 阶段五冻结验收、完整人工配对与三轮指标 | 探针数据/目的不同，不能替代P07 | 五覆盖AC20及完整AC07—10/21/27语义验收，保留失败不降低阈值 |
| Linux VM Compose、完整导出和整体演示 | 当前镜像入口/扩展/静态产物尚有交付差距 | 五执行AC01/18/23/24相关交付；本清单只验证Windows原生，不部署 |
| 依赖粒度与串行面 | 当前上游第1节规定二A依赖先于二B实施，本清单继续保留D0/D1/D2/D3进入门槛；7—11沿用6的真实存储门禁 | 当前按既有顺序推进，不要求用户重答前置是否成立；仅拟提前实施或变更阶段顺序时按F-3处理。D5/D7/D8只阻塞其使用任务，不反向阻塞准备任务；D6在6前就绪即可 |
| `--full-suite`取证与脚本单一超时 | 脚本pytest子进程上限180秒为硬编码，全量档与单场景档未区分 | 任务6交付分档超时并登记；未分档前24不得宣称全量档取证；不得改小测试范围充数 |
| 任务1/26判定与归类依据 | v1.1把正常证据核对/探针分类增加为逐次确认，超出上游既定推进规则 | 本次移除重复门槛；1核对现有批准和证据，26依上游规则留存分类依据、影响及可复核报告；硬门禁立即阻断，争议或规则/授权变更才提请用户裁决，不以签署人未定作为新阻塞 |
| 剩余待定路径/工具 | DTO生成工具、MCP服务物理位置、开发数据位置、交付记录文件名为开工核对项；迁移文件名与报告名已给默认值 | 开工时核对后写入交付记录与修订记录，不得静默改语义；无法确定则按缺口记录，不临时自造第二套 |
| 现有无关基线失败 | 本次未跑测试，不清楚是否仍存在历史脚手架失败 | 24如实分类记录，33不能将局部通过写全套通过；不删除/回滚用户代码 |

## 7. 上游决定边界与适用规则

保留F-1—F-6标识供追溯，但不把每项都当成尚待确认的阻塞。既有业务规则及推进条件直接适用；只有实际授权缺失、结论争议，或拟变更已确认的能力/阶段边界时才请求相应决策。以下记录不授权本次实施或真实调用。

| 编号 | 事项与当前规则 | 触发位置 | 适用行为与阻塞条件 | 决策边界 |
| --- | --- | --- | --- | --- |
| F-1 | 条件性方案变更：任务5/6若证实无法在提交边界fencing，拟放弃受控续跑将改变既有选择 | 任务5、6、11 | 未取得fencing证据不得开启11续跑；先按上游保持待核实，不需要事先决定放弃能力；只有实际提出替代方案时才裁决，不用SQLite/跳过降级 | 用户决定是否改变2026-11-10已确认能力；本清单不代作决定 |
| F-2 | 已明确：按架构第7.1节三类结果归类，不新增冻结验收阈值或逐次签署 | 任务26、27、28、30 | 26登记实际证据及影响任务；基本不可行先解决，硬门禁立即阻断；语义差距留痕后可继续已授权工程。争议未解决时不按可继续处理；27在25/26后执行，不再声明与25并行 | 正常判定沿用既定规则；只有争议或拟改规则/授权范围时提请用户，不把“签署人未定”当门槛 |
| F-3 | 既有顺序直接适用：D0/D1/D2/D3先于二B实施；若拟提前4/5属于另一次范围变更 | 任务1、4、5、6 | 当前保留既有进入顺序；D5适配由5/6产出、D6环境在6前核实，不自循环。仅准备待审草案不代表准许提前实现 | 无需重问既定顺序；实际申请改变阶段顺序时由用户/架构评审决定 |
| F-4 | D0实际批准与实施授权仍需存在，但不新增计划外签署角色或重复进入批准 | 任务1、2 | 1记录可核查的上游评审结论、用户实施范围和外部前置证据；缺真实授权则实现继续阻塞；满足既定条件后按授权执行，后续内部成果未交付不阻塞1本身 | 用户授权及明确委托的评审结论据实记录；不能从当前“修复计划”指令推导实施批准 |
| F-5 | 真实模型端点/数据类别/调用次数/预算及显式调用授权由专项计划固定 | 任务25、26 | 25可离线准备计划/所选样例并送复核，不等授权才准备；26在数据复核、专项计划固定和明确调用授权后执行，不静默沿用已有配置 | 用户决定实际调用范围；不要求另造上游未规定的票据系统 |
| F-6 | 二A完成以实际交付证据为准，不要求先承诺日历日期才能编制或核对二B计划 | D1/D2/D3、任务1 | 缺二A证据保持相关实现阻塞；取得证据后按1核对，不需再次确认既定依赖是否成立 | 二A交付与评审主体提供证据；只有计划范围或依赖变化才更新本清单 |

## 8. 修订记录

每次后续修改追加一行，不覆盖旧行；触发原因限定上游方案修订、可追溯评审意见、范围或依赖变化。任务编号新增/修改/作废必须明确，不能重用已作废身份而丢历史。

| 版本 | 日期 | 触发原因 | 变更摘要 | 影响的任务编号 |
| --- | --- | --- | --- | --- |
| v1 | 2026-11-10 | 范围或依赖变化：用户本次要求将二B v0.1-review-draft拆为单独审查/验收清单；工作区已纳入总体架构v0.2探针路线，二A依赖和指定_docs目录仍未就绪 | 新增33项任务，分开离线开发与真实PG/MCP/模型/浏览器取证，逐项映射上游增量/AC及阻塞；按不建目录约束交付到已有docs路径，避免伪称前置完成或启动实施 | 新增1—33；修改无；作废无 |
| v1.1 | 2026-11-10 | 可追溯评审意见：对本文件v1的只读评审R-01—R-20及上游方案/架构/AC交叉核对 | 补任务22落稿权限与来源重验（P0）、任务6/24分档超时与全量档取证口径（P0）；把条件前置26写入27/28/30前置列；修§2“每项单归属”与§4探针行矛盾；锁定迁移文件名、MCP stdio入口与启动方式、playwright版本；任务12崩溃注入手段、任务8启动容量/模式预检、任务13六类结局、任务4离线权限断言边界、任务31场景对应关系、任务33环境身份与报告名；新增§2.1环境矩阵、§7待上游决定F-1—F-6；修D1漏列domain、D4字段位置、AC19枚举 | 修改4、5、6、8、12、13、14、22、24、26、27、28、30、31、32、33；新增无；作废无；章节新增2.1、7，原“修订记录”改为第8节 |
| v1.2 | 2026-11-11 | 评审意见（来源：本聊天2026-11-11对v1.1的只读复评，项目约定轴3条/方案一致性轴3条，去重4类）；用户随后要求“修复和完善” | 删除正常探针归类/推进的重复确认门槛，沿用架构三类规则并只升级争议或规则变更；区分任务1外部进入条件与后续内部成果，消除6/25/31自前置；明确27在25/26后执行并同步28—30继承条件；补PowerShell源码路径与MCP/售前HTTP启动模板，保持13/31离线准备、14/32真实执行分离；同步D5/D7/D8、缺口表和F项，保留真实调用授权、上游待审状态及既有修订历史 | 修改1、6、13、25、26、27、28、29、30、31、32；新增无；作废无；同步第1/6/7节，新增2.2 |
| v1.2（技术复评登记） | 2026-11-11 | 评审意见（来源：本聊天用户明确要求完整复评v1.2并按结论登记状态；本轮项目约定轴、方案一致性轴及主核对均未发现阻断问题） | 追加第9节完整复评记录和上轮四类问题关闭证据，将状态登记为“已评审（AI技术复评通过；未执行，不构成实施授权）”；同步缺口表区分清单已评审与上游批准/实施授权，任务内容及既有历史不变 | 无；仅评审元数据、缺口状态说明及评审记录变化，1—33任务内容不变 |

| v1.2（2B-0续作登记） | 2026-11-11 | 用户批准B层PostgreSQL命令并要求继续中断的2B-0；开工核对任务3超800行及二A迁移占号 | 任务3先登记3a/3b内部审查切片并拆文件；任务4固定0006迁移及0005前驱；锁定saver版本及任务6并发/回滚取证范围；不扩展至2B-1 | 修改3—6的交付路径与审查粒度说明；任务1—33编号、业务规则及后续阶段边界不变 |

## 9. v1.2完整技术复评记录

### 9.1 日期、版本与评审依据

- 日期：2026-11-11（Asia/Shanghai）。
- 评审版本：本文件v1.2全文，覆盖33项任务及第1—8节；登记前文件SHA256为`7E78767BD0E2A25849AF57FF9DD7DA3B1131284E25815D78FA697B8A76DBD8CE`。本次登记只改变评审元数据、缺口状态说明并追加记录，不改任务内容，因此不另升实施内容版本。
- 评审方式：主审完整核对，加两条独立只读AI复核线，分别检查项目约定（Standards）和上游方案一致性（Spec）；主审汇总裁定。独立复核会话标识为`01a1c656-5fae-7071-9996-bb20f481ba52`、`01a1c657-2a6e-7060-ac19-32b9693e2c05`。
- 依据：[AGENTS.md](../../../AGENTS.md)、[CLAUDE.md](../../../CLAUDE.md)、[GLOSSARY.md](../../../GLOSSARY.md)、[STATUS.md](../../../STATUS.md)、[二B方案](../specs/阶段二B-Agent分析工作流设计方案.md) v0.1-review-draft全文、[总体架构](../specs/总体架构设计.md) v0.2-review-draft（特别第7.1节）、[双入口设计](../specs/双入口工作台与前端分阶段接入设计.md)及[需求基线](../specs/初版业务需求分析文档.md)的权限/状态/输出/资源边界和指定AC。
- 输入核实：上游两份设计的实际SHA256与开头记录一致；另静态核对售前目录、草稿存储约束、来源闭包、兼容checkpoint、HTTP路由、前端脚本和数据库验证脚本，与计划现状描述一致，不作为运行结果。
- 历史边界：本轮可核对的是当前v1.2、聊天中的v1.1复评四类问题和修订记录；没有独立v1.1 Git快照，原R-01—R-20完整意见亦未附入。不伪造Git差异，也不声称原20项历史意见逐条全部关闭。

### 9.2 上轮问题关闭情况

| 问题 | v1.2复核位置与关闭证据 | 结论 |
| --- | --- | --- |
| 重复逐次确认门槛 | 第1节、任务26及F-2/F-4沿用上游三类结局规则：语义差距留痕后可继续已授权工程，硬门禁立即阻断；仅争议/规则或授权变化才升级裁决。D0实施授权及真实调用授权继续保留 | 已关闭；未用移除重复门槛代替实际授权 |
| 任务1/6/25/31把内部成果当自身前置 | D5/D7/D8和第1节明确“外部进入前置”与“内部交付门禁”；6取得D5真实证据，25准备/送复核样例，31准备入口；26/32分别在使用时核实真实执行条件 | 已关闭；D1/D2/D3等真实外部依赖仍按原顺序阻塞实施 |
| 任务27依赖26却又声称与25并行 | 任务27明确在25/26之后执行；任务28—30同步继承26结论条件；任务25不再保留与27并行声明 | 已关闭；显式任务依赖无自引用或向后引用 |
| MCP启动模板缺源码导入路径 | 第2.2节及任务13/31提供PowerShell的`PYTHONPATH`设置、`uv run python -m presales.infrastructure.opportunity_workspace`及售前uvicorn模板，注明由后续真实验证任务使用 | 已关闭；只验证模板语法和导入配置依据，未启动服务或声称新入口已实现 |

### 9.3 全文复评结论与检查证据

| 复核线 | 范围 | 结论 |
| --- | --- | --- |
| 项目约定轴（Standards） | 命名/事实状态、33项连续编号及11字段、单增量归属、改动粒度边界、真实服务独立取证、阻塞与命令口径、追加历史 | 未发现阻断问题；上轮四类整改均有对应文本，未发现需新增整改的问题 |
| 方案一致性轴（Spec） | 全任务顺序、D0—D8与F-1—F-6、AC/增量/额外反例覆盖、Attempt恢复/取消/来源/落稿、SSE/运行页、真实探针及跨阶段边界 | 未发现阻断问题；指定7项主AC及10组额外检查项均有任务承接，未发现实质缺失、错误实现要求或范围扩张 |
| 主审及文档结构核对 | 全文与当前目录/源码交叉核对，复核两条报告和输入指纹 | 同意两轴结论；33项各11个非空字段，主归属为2B-0六项、2B-1六项、2B-2十四项、2B-3六项、退出评审一项；前置编号关系及相对链接检查无错误 |

本轮允许执行的文档检查为`.venv/Scripts/pymarkdown.exe scan docs/superpowers/plans/Phase-Two-B-Implementation-v1.md`，使用项目已安装的pymarkdown及配置，扫描通过。另以只读文档脚本核对编号/字段/显式前置关系/链接，并用PowerShell解析器检查第2.2节两段模板语法，均未发现错误。它们不执行模板、产品代码或业务测试，不能证明所列工程验收通过。

### 9.4 遗留项及适用限制

| 遗留项 | 性质 | 后续处理 |
| --- | --- | --- |
| 上游方案仍待评审、实施范围及真实调用尚未授权 | 实施前置，非本清单的技术评审阻断缺陷 | 任务1核对D0；真实模型由26核实专项授权；本次不开始任何任务 |
| 二A-2/二A-3/二A探针尚未取得交付证据 | 外部能力依赖，已明确阻塞相关实施 | 按D1/D2/D3及任务1处理，不用Fake解除 |
| saver提交边界、数据库权限/版本、MCP进程及浏览器环境未作真实验证 | 实施期技术门禁，不能由静态评审预先保证 | 分别由6/12/14/24/26/32取得真实证据，失败按既定规则阻断对应执行 |
| 任务行数为估计，生成工具/专项配置和少数路径需开工核定 | 已记录工程选择，非当前业务规则缺口 | 按第2/6节执行，超粒度先修订再拆分，不顺手扩范围 |
| 指定`_docs`目录不存在、旧版完整评审证据不可取得 | 已显式披露的路径偏差与历史追溯限制 | 保留现有单文件路径及上述历史边界，不新建目录、不虚构原意见关闭情况 |

最终结论：**v1.2实施任务清单的AI技术复评通过，无阻断问题，登记为“已评审（AI技术复评通过；未执行，不构成实施授权）”。** 本结论只适用于所记录输入版本的计划完整性、一致性及可审查性；不批准上游整体方案，不宣称产品能力、工程测试、真实模型质量或任何阶段验收通过，不解除第1/6/7节中的实施门禁。后续任务内容或上游约束变化时，须追加修订及相应复评记录。
