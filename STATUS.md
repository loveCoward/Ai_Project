# 项目状态

更新日期：2026-11-12（Asia/Shanghai）

本文件只记录当前可核验的事实，不替代需求基线。

| 维度 | 当前状态 | 事实边界 |
| --- | --- | --- |
| 需求 | 已冻结 | v0.1 已于 2026-11-01 获批，Q1-Q45 与 P01-P09 纳入冻结基线。基线见 `docs/superpowers/specs/初版业务需求分析文档.md`。 |
| 设计 | 方向已确认，整体评审未完成 | TD-001至TD-034方向及滚动规划保留；双入口路线已确认。二A方案为v1.1，前端基础安排在二A、SSE安排在二B。2A-1工程实现不等于设计评审通过；整体技术设计、二A阶段退出条件及阶段一验收仍未批准。 |
| 开发 | 阶段一、2A-0至2A-3工程基础、2B-0、2B-1任务7至12及2B-2任务13已有代码 | 2A-1材料解析、输入清单、知识版本与审核、索引构建及持久化使用迁移`0004_knowledge`，证据见[2A-1交付记录](docs/maintenance/phase2a1-delivery-2026-11-06.md)，属历史结果。2026-11-11已实现2A-2共享受限检索/只读Product Catalog MCP及2A-3最小材料、知识审核、检索、证据定位页面与业务HTTP入口；历史工程和浏览器验证见[二A外部前置交付记录](docs/maintenance/phase2a-prerequisites-delivery-2026-11-11.md)，本次未重跑，二A整体未验收。2B-0已实现Proposal Analysis Attempt契约、Proposal Package Schema 1.0、持久化执行记录及受控checkpoint提交适配（迁移`0006_proposal_attempt`）。2B-1任务7至12已实现Attempt授权边界与主动预算持久累计、显式模型传输适配、单事务启动命令与`analyzing`状态迁移`0007`、监督器领取/心跳/关闭及售前ASGI生命周期装配、架构师取消与执行停止后条件排空（`0008`）、重启核实与受控继续（`0009`），并由任务12完成真实子进程并发、进程终止与旧代次迟到写入验证，修复两个真实产品缺陷。2B-2任务13已实现商机只读背景读取与只读stdio服务入口（迁移`0010_opportunity_background`），含背景必需性三态、七类读取结局、60000字符/200条容量上限拒绝而不截断，以及启动时背景字符计入关键输入容量预检。各任务的实现范围、用例映射、A/B/C分层与测试数字不在本表重复，统一见下方「交付记录索引」。尚未开放2B分析运行时REST命令；未实现分析图与语义评测、SSE运行页、阶段三方案审批、阶段四发布与MCP写入；stdio真实进程往返与凭证隔离验证留任务14；提交事务瞬间崩溃窗口未覆盖；二B整体未验收。 |
| 前端 | 2A-0基础与2A-3最小材料/知识工作台已实现；分析工作台未实现 | React 19/TypeScript/Vite已实现登录、身份校验、受保护路由、登出、草稿/材料读取，以及材料处理、知识审核、受限检索和证据定位页面；业务API与历史浏览器流程验证见[二A外部前置交付记录](docs/maintenance/phase2a-prerequisites-delivery-2026-11-11.md)。token仅存内存，刷新需重新登录；未建立持久浏览器E2E套件，无分析运行页面或SSE订阅，无正式方案审批/发布页面。`frontend/dist/`是构建产物，本次未重跑typecheck或build。 |
| 评测 | 未完成 | 尚未创建并复核黄金数据集，尚未执行P07冻结指标下的真实模型能力评测或产品验收。2026-11-11二A真实检索探针和2B-0工程验证均不构成P07能力评测；2A-1的Fake Embedding只证明流程、契约、权限与恢复，不证明检索语义质量。 |

状态变更必须以明确授权和可核验产出为依据，并同步更新本文件。

**交付记录索引**（各任务的实现范围、用例映射、A/B/C分层与测试数字以记录为准，本表不重复）：当前UI路线与阶段安排见[双入口接入设计](docs/superpowers/specs/双入口工作台与前端分阶段接入设计.md)；阶段一历史验证记录见[实施清单](docs/superpowers/plans/2026-11-03-phase-one-implementation.md)；2A-1交付与验证边界见[交付记录](docs/maintenance/phase2a1-delivery-2026-11-06.md)；2B-0工程实现与历史验证边界见[交付记录](docs/maintenance/phase2b-0-delivery-2026-11-11.md)；2B-1任务7至12及2B-2任务13的验证边界见[任务7交付记录](docs/maintenance/phase2b-1-task7-delivery-2026-11-11.md)、[任务8交付记录](docs/maintenance/phase2b-1-task8-delivery-2026-11-11.md)、[任务9交付记录](docs/maintenance/phase2b-1-task9-delivery-2026-11-11.md)、[任务10交付记录](docs/maintenance/phase2b-1-task10-delivery-2026-11-12.md)、[任务11交付记录](docs/maintenance/phase2b-1-task11-delivery-2026-11-12.md)、[任务12交付记录](docs/maintenance/phase2b-1-task12-delivery-2026-11-12.md)及[任务13交付记录](docs/maintenance/phase2b-2-task13-delivery-2026-11-12.md)。本工作区最近一次`tests/presales`离线重跑为2026-11-12的282通过/94跳过（根目录全量离线未重跑）；同日真实PostgreSQL全量档重跑为567通过/5跳过；本次未重跑前端typecheck/build、Docker或真实模型评测。
