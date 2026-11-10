# AI 辅助开发约定

## 项目定位与当前状态

- 本项目正在将继承的通用 Agent 服务脚手架改造成企业 AI 售前方案工作台。
- 当前代码包含FastAPI/LangGraph/Streamlit脚手架、阶段一数据/授权基础、2A-0最小React工程（登录、身份校验、受保护路由、草稿/材料受限读取）、2A-1材料解析/知识治理/索引构建的应用服务与持久化，以及少量售前HTTP读取入口；核心业务前端、检索、Agent分析、审批发布和MCP写入尚未实现，不能笼统称已完成产品能力。
- 2A-1目前是Python应用服务与数据库能力，未新增业务HTTP路由或浏览器页面；2A-1交付记录只能证明该范围内的工程实现和本机数据库专项验证，不代表二A整体完成、产品验收、部署批准或真实语义质量达标。
- 已确认UI方向：Streamlit用于聊天演示/内部验证，React + TypeScript + Vite用于核心工作台，REST处理业务操作、SSE传递受限进度。2A-0前端基础已存在；2A-3材料/知识页面、二B的SSE与后续业务页面仍按`docs/superpowers/specs/双入口工作台与前端分阶段接入设计.md`推进，存在工程代码不等于阶段验收。
- 业务需求已经形成 v0.1 基线，但需求批准不等于功能已实现、测试已通过、允许部署或允许调用真实模型。
- 所有实现判断都必须以当前代码和测试结果为准，不得把 README、需求文档、交付记录或目录结构中的规划描述当成现有能力；交付记录中的“建议标注完成”不自动更新本文件或`STATUS.md`。

## 需求与术语基线

- 唯一业务需求基线：`docs/superpowers/specs/初版业务需求分析文档.md`。
- 需求决策索引：`docs/presales-requirements-decisions.md`。
- 领域术语以 `GLOSSARY.md` 为准；新增代码、测试和文档应沿用其中的对象名称和状态含义。
- 需求基线中的权限、任务/版本状态、审批、证据来源、派生物访问边界、发布恢复、数据真实性和验收规则均属于已批准约束。
- 若实现需要改变业务规则，先记录需求变更及影响并取得明确确认，不在代码或测试中悄悄改写基线。

## 开始工作前

1. 先阅读本文件、`CLAUDE.md`、相关需求章节和 `GLOSSARY.md`。
2. 检查当前代码、测试和工作区状态，确认目标行为是否已经存在以及是否有用户未提交的修改。
3. 区分三类内容：已批准需求、当前已实现行为、尚未授权的实现方案；不要用其中一类替代另一类。
4. 变更完成后说明实际实现范围、未实现部分和验证结果，不把规划、Stub 结果或局部通过描述为产品验收通过。

## 开发边界

- 不把继承脚手架的聊天、Agent、RAG 或 MCP 示例能力描述为已完成的售前业务能力。
- 默认工程测试必须可在无真实模型 API Key 的情况下运行；Fake/Stub 只用于流程、契约、权限、失败和恢复测试。
- 真实模型调用和 Agent 能力评测必须显式启动、记录模型与运行模式，并与工程自动化结果分开；不得静默以 Fake 替代失败的真实运行。
- 只使用合成客户材料、获授权的公开资料或自有资料；不得读取、提交或记录真实客户敏感材料、密码、JWT、API Key 或其他秘密。
- 任何外部写入、发布、审批、权限放宽或跨来源数据访问都必须遵守需求基线中的授权和恢复规则，不能只依赖前端隐藏按钮。
- 不修改用户已有工作区变更，不清理或重置与当前任务无关的文件；提交前核对差异范围。

## 代码库地图

- `src/`：当前 Python 应用代码；`service/` 提供服务入口，`agents/` 放置 Agent 图和工具，`schema/` 放置数据模型，`core/` 放置配置与模型装配，`memory/` 放置持久化适配，`client/` 和 `streamlit_app.py` 提供继承脚手架的客户端与聊天界面。
- `src/presales/`：售前业务代码；`api/` 提供登录、JWT主体解析及材料/草稿读取路由，`application/` 提供应用服务，`access/` 提供授权与JWT，`domain/` 提供契约，`infrastructure/` 提供配置、数据库、文件与外发适配，`materials/`、`knowledge/`、`indexing/` 分别提供2A-1解析、知识治理和索引构建，`maintenance/` 提供本机维护命令。这些是Python服务能力，不等于已有对应HTTP接口或页面。
- `frontend/`：React + TypeScript + Vite 最小工作台（2A-0）；`src/` 为源码，`dist/` 为构建产物，不得把构建产物或目录存在当作业务能力证据；前端没有 Node 业务服务端，也不承担授权判断。
- `migrations/`：Alembic 迁移，当前包含身份、领域、完整性和知识（`0004_knowledge.py`）版本；迁移必须显式执行。
- `tests/`：按模块组织的 pytest 测试，包含服务、Agent、Schema、客户端、语音、持久化、Docker集成和`presales/`售前专项测试；前端目前没有单元测试或浏览器E2E。
- `docs/`：运行说明、维护资料、交付记录、业务决策和需求基线；业务规则文档优先放在现有目录结构中，实现状态以`STATUS.md`和可核验代码/测试为准。
- `scripts/`：冒烟测试、UI 端到端测试、模型检查和其他辅助脚本；`test_presales_postgres.py` 会创建临时本机PostgreSQL实例，需要真实服务、浏览器、Docker或模型的脚本不属于默认离线单元测试。
- `docker/`、`compose.yaml`：服务和 Streamlit 镜像及本地依赖编排。
- `.env`、`privatecredentials/`、`checkpoints.db` 等本地运行产物或凭据文件不得进入版本控制或文档示例。

## 常用命令

```sh
uv sync --frozen
uv run pytest
uv run pytest tests/presales -q
uv run ruff check
uv run ruff format --check
uv run pyrefly check
uv run pymarkdown scan AGENTS.md STATUS.md README.md docs/
```

真实数据库专项验证（需要本机 PostgreSQL 二进制；脚本自行创建临时回环实例）：

```sh
uv run python scripts/test_presales_postgres.py --pg-bin "<PostgreSQL bin>" --check-vector --full-suite --pytest-args -q -rs
```

前端工程检查（在 `frontend/` 下执行；不启动 Node 业务服务，不需要模型 Key）：

```sh
cd frontend
npm ci
npm run typecheck
npm run build
```

## 测试与验证要求

- 代码修改至少运行与改动范围匹配的 pytest；影响共享模块或配置时优先运行完整 `uv run pytest`。
- 涉及权限、来源过滤、派生物、审批、自审批、状态转换、取消、幂等、未知结果或发布恢复时，必须补充或更新对应工程测试。
- 售前 PostgreSQL 专项用例默认按既有 opt-in 规则跳过；声称 PostgreSQL 门禁通过前必须用 `scripts/test_presales_postgres.py` 在真实临时实例运行，不能用 SQLite、Fake 或只跑离线单测替代。
- 前端修改至少运行 `npm run typecheck` 和 `npm run build`；前端目前没有单元测试或浏览器E2E，构建通过不能替代业务流程、认证到期和越权验证。
- 涉及 Docker、持久化、服务启动、Streamlit 或外部集成时，按现有测试和 `.claude/skills/` 中的维护说明选择额外检查。
- 引用历史交付记录中的测试数字（例如2A-1的`318 passed`）时必须标注为历史结果；未重跑前不能表述为本次通过。
- 真实能力评测使用冻结数据、明确的真实模型/本地模型和独立报告；工程测试通过不能替代能力评测。
- 失败、降级、部分成功和未验证状态必须在测试结果中明确记录，不得通过删除失败样例或切换模式制造成功结论。

## 文档协作规则

- 业务规则变更先更新需求基线或决策记录，再修改实现和测试；实现细节不能反向伪造需求批准。
- 机器 JSON Schema、黄金数据集、评测阈值、验收结果和部署结论必须明确标注其状态，不得把计划或目标写成已完成事实。
- 新增文档优先放入现有 `docs/` 目录，并保持与 `GLOSSARY.md` 一致；避免复制整份需求文档造成规则漂移。
- 文档、导出内容和示例数据不得包含秘密或真实客户敏感信息。

## 与 CLAUDE.md 的关系

- `AGENTS.md` 负责本项目业务化改造的上下文、AI 辅助开发边界、验证要求和文档协作规则。
- `CLAUDE.md` 保留现有代码注释风格及维护脚手架边界；编辑代码时同时遵守两份文件中不冲突的约定。
- 不重复搬运或重构 `CLAUDE.md`；两份文件出现冲突时，优先保留更具体且适用于目标文件的现有项目约定，并在必要时先澄清规则归属。
