# 项目状态

更新日期：2026-11-12（Asia/Shanghai）

本文件只记录当前可核验的事实，不替代需求基线。

| 维度 | 当前状态 | 事实边界 |
| --- | --- | --- |
| 需求 | 已冻结 | v0.1 已于 2026-11-01 获批，Q1-Q45 与 P01-P09 纳入冻结基线。基线见 `docs/superpowers/specs/初版业务需求分析文档.md`。 |
| 设计 | 方向已确认，整体评审未完成 | TD-001至TD-034方向及滚动规划保留；双入口路线已确认。二A方案为v1.1，前端基础安排在二A、SSE安排在二B。2A-1工程实现不等于设计评审通过；整体技术设计、二A阶段退出条件及阶段一验收仍未批准。 |
| 开发 | 阶段一、2A-0至2A-3工程基础、2B-0及2B-1任务7至10已有代码 | 2A-1材料解析、输入清单、知识版本与审核、索引构建及持久化使用迁移`0004_knowledge`，证据见[2A-1交付记录](docs/maintenance/phase2a1-delivery-2026-11-06.md)，属历史结果。2026-11-11已实现2A-2共享受限检索/只读Product Catalog MCP及2A-3最小材料、知识审核、检索、证据定位页面与业务HTTP入口；历史工程和浏览器验证见[二A外部前置交付记录](docs/maintenance/phase2a-prerequisites-delivery-2026-11-11.md)，本次未重跑。真实Embedding探针观察到基本链路，但样例性质、精排和P07质量评测边界见该报告，二A整体未验收。2B-0已实现Proposal Analysis Attempt契约、Proposal Package Schema 1.0、持久化执行记录及受控checkpoint提交适配，使用迁移`0006_proposal_attempt`（前驱为二A的`0005_retrieval`）；验证结果见[2B-0交付记录](docs/maintenance/phase2b-0-delivery-2026-11-11.md)及[结构化验证摘要](docs/maintenance/phase2b-0-verification-2026-11-11.json)，均为2026-11-11历史结果，本次未重跑：离线全量341通过/82跳过，真实PostgreSQL全量418通过/5跳过。2B-1任务7已实现Attempt授权边界、主动预算持久累计与显式模型传输适配（离线专项21通过）；任务8已实现单事务启动命令与`analyzing`状态最小权限迁移`0007_proposal_attempt_start`（离线专项25通过）；任务9已实现Attempt监督器领取/心跳/启动扫描/关闭及售前ASGI生命周期装配（离线专项26通过）；任务10已实现架构师取消命令、任务`cancelled`最小权限迁移`0008_proposal_attempt_cancel`、旧执行fencing与执行停止后条件排空/槽位释放（离线专项13通过）。四者证据分别见[任务7交付记录](docs/maintenance/phase2b-1-task7-delivery-2026-11-11.md)、[任务8交付记录](docs/maintenance/phase2b-1-task8-delivery-2026-11-11.md)、[任务9交付记录](docs/maintenance/phase2b-1-task9-delivery-2026-11-11.md)及[任务10交付记录](docs/maintenance/phase2b-1-task10-delivery-2026-11-12.md)；真实数据库竞态、进程崩溃、跨进程租约接管与取消/落稿真实竞争未验证。尚未开放2B分析运行时REST命令、完整恢复消费，未实现2B分析图与语义评测、SSE运行页、阶段三方案审批、阶段四发布与MCP写入；二B整体未验收。 |
| 前端 | 2A-0基础与2A-3最小材料/知识工作台已实现；分析工作台未实现 | React 19/TypeScript/Vite已实现登录、身份校验、受保护路由、登出、草稿/材料读取，以及材料处理、知识审核、受限检索和证据定位页面；业务API与历史浏览器流程验证见[二A外部前置交付记录](docs/maintenance/phase2a-prerequisites-delivery-2026-11-11.md)。token仅存内存，刷新需重新登录；未建立持久浏览器E2E套件，无分析运行页面或SSE订阅，无正式方案审批/发布页面。`frontend/dist/`是构建产物，本次未重跑typecheck或build。 |
| 评测 | 未完成 | 尚未创建并复核黄金数据集，尚未执行P07冻结指标下的真实模型能力评测或产品验收。2026-11-11二A真实检索探针和2B-0工程验证均不构成P07能力评测；2A-1的Fake Embedding只证明流程、契约、权限与恢复，不证明检索语义质量。 |

状态变更必须以明确授权和可核验产出为依据，并同步更新本文件。

当前UI路线与阶段安排见[双入口接入设计](docs/superpowers/specs/双入口工作台与前端分阶段接入设计.md)；阶段一历史验证记录见[实施清单](docs/superpowers/plans/2026-11-03-phase-one-implementation.md)；2A-1交付与验证边界见[交付记录](docs/maintenance/phase2a1-delivery-2026-11-06.md)；2B-0工程实现与历史验证边界见[交付记录](docs/maintenance/phase2b-0-delivery-2026-11-11.md)；2B-1任务7至10离线验证边界见[任务7交付记录](docs/maintenance/phase2b-1-task7-delivery-2026-11-11.md)、[任务8交付记录](docs/maintenance/phase2b-1-task8-delivery-2026-11-11.md)、[任务9交付记录](docs/maintenance/phase2b-1-task9-delivery-2026-11-11.md)及[任务10交付记录](docs/maintenance/phase2b-1-task10-delivery-2026-11-12.md)。本工作区最近一次离线重跑为2026-11-12的全量426通过/82跳过；本次未重跑真实PostgreSQL、前端typecheck/build、Docker或真实模型评测。
