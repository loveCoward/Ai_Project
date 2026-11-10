# 阶段 2A-1 实施交付记录

日期：2026-11-06（Asia/Shanghai）。范围为《阶段二任务清单》任务 3、4、5、6，即 2A-1A 至 2A-1D。本文记录工程实现和测试事实，不修改需求、设计或 STATUS.md，不代表产品验收、部署批准或真实语义质量达标。

## 本次交付说明

### 目标与实际能力

沿用阶段一的可信 Actor、Policy、来源闭包、资源/版本身份、文件摘要与就绪、授权修订、PostgreSQL 短事务、追加审计和本地授权维护机制。新增以下能力：

- PDF 逐页、DOCX 段落/表格单元、Markdown 标题/段落及 JSON Pointer 路径解析；受控子进程最长 60 秒；不支持扫描 PDF、加密/损坏文件和不支持格式，不启动 OCR。
- 解析作业重试复用身份；失败记录追加；成功结果作为有来源依赖的不可变派生版本保存。期间撤权、文件损坏和超限不返回成功正文。
- 任务输入清单保存必需、可选、未分类标记及标记人/输入修订；新增文件保留已有人工标记，新文件默认未分类。必需/未分类失败保持待补充，可选失败明确列为未使用来源。
- 内部知识文档系列、内容版本、来源声明、产品/部署适用范围及候选/待审/发布/被替换/停用状态；客户材料不自动转为知识。
- 按段落/单元边界切块；默认最多 600 个确定性 Unicode 中文字/词/标点 token、80 token 重叠。该 token 口径明确记录，不宣称等同某个真实模型的 tokenizer。
- 独立索引作业记录解析器/分块/分词配置、模型身份、维度、模式和期限；在事务外生成 Embedding，完整物化后标记 ready；ready 不等于 published。
- 显式确定性 Fake Embedding，以及从受控配置读取目的端的真实适配器。真实适配器只以 MockTransport 测契约，没有真实调用。
- 架构师提交固定版本/构建/范围/预期修订，审核人独立决定发布、显式替换、停用；禁止自审批，包括提交后角色变更场景。
- 审核人可留痕调整候选适用范围；调整使旧审核失效，不改变已发布版本的范围。旧目录修订的审核失败后，可基于当前修订重新提交。
- 发布最终短事务复核当前权限、版本与目录修订、完整构建、范围冲突、100 版本/5,000 有效块上限；目录指针、治理状态、决定、变化事件和审计原子提交，失败保留旧目录。

### 当前使用流程

本次交付 Python 应用服务及持久化能力，未新增业务 HTTP 路由或浏览器页面。后续 API/页面调用同一应用服务，不另造授权路径。

1. 架构师通过已有 PresalesService 创建任务、登记已声明且就绪的材料。MaterialService.set_manifest 保存材料必要性；parse_material 逐文件解析，get_parse_result/input_status 返回结果或待补充原因。
2. 内部知识通过 KnowledgeRegistry.create_document 建立系列。资源授权沿用本机维护服务，由维护者显式授予架构师、审核人和读者；创建文档不自动授予全局访问权。
3. register_version 登记合规来源、固定文件摘要、产品/部署范围；MaterialService 解析该知识版本。inspect 返回治理状态、版本修订和目录修订。
4. 显式选择 Fake 或已配置真实适配器，IndexService.build 构建独立候选索引；status/cancel 读取或取消，未完成构建不会进入目录。
5. KnowledgeReview.submit 绑定固定版本、构建及预期修订，decide 由另一名当前有权审核人执行。冲突需 adjust_candidate_scope 或显式 replace 解决。
6. published 只给出当前有权、就绪、适用、无未解决重叠冲突的发布目录身份；这是目录门禁，不是 2A-2 的向量/BM25 检索。停用从目录立即移除，历史解析与版本仍按当前权限读取。

### 关键数据与规则

- 所有客户/知识测试资料均为 synthetic。受限正文不写审计和幂等回执；审计记录身份、操作和结果，用户填写的原因仍应避免敏感正文。
- 不接受模型提供的来源页码、角色、授权范围或接收端。知识版本与解析结果有稳定 UUID；来源定位和块身份由程序生成。
- 文件边界为单文件 20 MiB、单任务 50 MiB/10 文件、单文件 100,000/单任务 300,000 Unicode 解析字符，超限失败而不裁剪。
- DOCX 限制 2,048 ZIP 条目和 50 MiB 展开量，拒绝实体/DOCTYPE；不提取链接内容。解析进程只继承必要环境，不继承模型或数据库秘密。
- 文档版本计数包含候选、发布、被替换和停用；重试解析/重建不增加知识版本；历史不硬删除。
- 有效块按整个当前目录计数，不按读者权限缩减；替换先扣旧构建再加新构建。并发决定锁定目录修订，不能各按旧计数提交。
- 默认单请求 60 秒、整批 600 秒、技术错误最多三次尝试；每次重试和完成前重新验权，重试不重置总期限。测试可以注入更短期限以验证超时，未修改默认测试配置或既有超时。
- PGVector 使用原生 vector 列和 SQL cast，直接通过已安装 psycopg/SQLAlchemy，不新增 Python pgvector 依赖，不增加近似索引。

### 修改前后场景

一个合成商机有必需的需求 Markdown 和可选扫描 PDF。修改前系统只保存原文件及 readiness；现在 Markdown 有固定段落出处，扫描 PDF 明确失败，可选失败列为未使用，必需失败则任务待补充。内部合成产品文档解析、索引成功仍只是候选；独立审核后才进入当前目录。新版本构建/审核失败保留旧目录；显式替换成功切换目录；停用立即排除且旧内容不被改写。

## 验收依据与退出证据

| 设计/清单要求 | 对应证据 | 验证边界 |
| --- | --- | --- |
| AC02：PDF/DOCX/MD/JSON 定位与扫描件失败 | test_parsing.py、test_material_manifest.py | 合成原生文件及真实解析子进程；无 OCR |
| 必需/可选/未分类逐文件失败与待补充 | test_material_manifest.py、test_phase2a_boundaries.py | 人工分类保留；可选不自动污染有效结果 |
| 字节、文件数、Unicode 字符、DOCX 展开边界 | test_parsing.py、test_phase2a_boundaries.py | 正反边界；50 MiB 任务文件量和 300,000 字符均实际构造 |
| 超时、重试身份、成功结果不可覆盖、撤权后重放 | test_material_manifest.py、test_phase2a_recovery.py | 子进程终止与持久化失败分别验证；成功重放不再次解析 |
| AC03：版本、范围、权限、来源，候选不进入发布目录 | test_knowledge_registry.py、test_knowledge_schema.py、test_index_build.py | 不代表已经完成正式检索器 |
| 第 100/101 版本、历史版本保留、重建不重计 | test_knowledge_registry.py、test_index_build.py | 真实 PostgreSQL 登记 100 个不同版本 |
| 索引半成品、错误、超时、取消不进入目录 | test_index_build.py、test_phase2a_recovery.py | Fake/Stub 注入技术失败；真实数据库验证状态与原子物化 |
| 5,000 块、替换净计数、并发容量 | test_phase2a_boundaries.py、test_knowledge_capacity.py | 实际写入 5,000 条 native vector；4,998→4,999 替换，两个并发发布仅一个可提交，净总量 5,000 |
| 独立审核、禁止自审批、旧审核拒绝 | test_knowledge_review.py、test_knowledge_publish.py、test_phase2a_boundaries.py | 含角色变更后自审批、范围修订后重放 |
| 并发替换/停用、失败保留目录、恢复 | test_knowledge_publish.py、test_knowledge_capacity.py、test_phase2a_boundaries.py | 真实锁/事务；审计权限失败导致整体回滚，恢复后可重新批准 |
| AC16 本阶段门禁：停用与当前授权收紧 | test_knowledge_publish.py、test_knowledge_capacity.py、test_phase2a_recovery.py | 当前目录立即移除，历史读取/决定回执重新验权；检索缓存实测属于 2A-2 |
| 目的端固定、禁重定向、模式和模型身份 | test_embedding_adapter.py、test_index_build.py | MockTransport/Fake；零真实外发，不证明语义质量 |

## 修改文件及作用

以下仅包含本任务文件；路径以仓库根目录为基准。

| 文件 | 作用 |
| --- | --- |
| migrations/versions/0004_knowledge.py | 新表、PGVector 0.8.6 检查、来源/维度/构建状态约束、追加记录保护、受限任务待补充函数与最小权限 |
| src/presales/materials/__init__.py | 材料包入口 |
| src/presales/materials/models.py | 定位、解析结果、必要性及字符上限契约 |
| src/presales/materials/parser.py | 格式分派和有界子进程 |
| src/presales/materials/parsers/__init__.py | 四类离线定位解析器 |
| src/presales/materials/worker.py | 受控解析进程入口 |
| src/presales/materials/service.py | 解析作业/结果/来源持久化、重试、任务输入清单、当前权限与就绪复核 |
| src/presales/knowledge/__init__.py | 知识包入口 |
| src/presales/knowledge/models.py | 产品/部署范围和规模常量 |
| src/presales/knowledge/registry.py | 系列/版本登记、幂等、统计、冲突及当前发布目录门禁 |
| src/presales/knowledge/review.py | 固定提交、独立审核、候选范围调整、发布/替换/停用原子切换及变化记录 |
| src/presales/indexing/__init__.py | 索引包入口 |
| src/presales/indexing/chunks.py | 可复现分块、中文确定性 token 与定位 |
| src/presales/indexing/embedding.py | 显式 Fake 和受控真实适配器 |
| src/presales/indexing/service.py | 独立构建、容量、期限、重试、取消、完整物化与权限复核 |
| tests/presales/phase2a_helpers.py | 合成登记/授权/构建辅助；仅在测试库为空时重置新增目录计数以隔离测试 |
| tests/presales/test_parsing.py | 四格式、扫描/加密/损坏、字符/ZIP/字节边界、子进程超时 |
| tests/presales/test_material_manifest.py | 必要性、重试、撤权重放、输入修订保留 |
| tests/presales/test_knowledge_schema.py | 适用范围契约 |
| tests/presales/test_knowledge_registry.py | 登记权限、100/101 版本、冲突及不可变历史 |
| tests/presales/test_index_build.py | 分块、模型/维度、构建失败与候选隔离 |
| tests/presales/test_knowledge_review.py | 提交/审核角色、拒绝及旧审核 |
| tests/presales/test_knowledge_publish.py | 发布/替换/停用、并发及范围调整 |
| tests/presales/test_phase2a_boundaries.py | 文件/字符/块边界、取消、自审批、审计回滚 |
| tests/presales/test_knowledge_capacity.py | 真实净计数、并发容量、回执撤权重放、旧审核不替换目录 |
| tests/presales/test_embedding_adapter.py | 仅 MockTransport 的受控真实适配器契约 |
| tests/presales/test_phase2a_recovery.py | 解析撤权、就绪收紧、部分批次/总期限、数据库不可变保护 |
| scripts/test_presales_postgres.py | 追加 --check-vector 与 --full-suite；保留原隔离、秘密处理、180 秒测试期限及清理逻辑 |
| pyproject.toml、uv.lock | 将已安装 pypdf 6.19.0、psycopg 3.3.4、SQLAlchemy 2.0.52、Alembic 1.20.0 精确锁定；离线锁定，不安装/新增依赖 |
| docs/maintenance/phase2a1-delivery-2026-11-06.md | 本交付说明及验证边界 |

## 实际验证

统一使用 --frozen --no-sync，避免 uv 在测试时安装或同步依赖；锁文件使用 uv lock --offline。真实 PostgreSQL 18.6/PGVector 0.8.6 运行在脚本创建的随机临时目录与回环端口，测试库名及 disposable marker 双重校验，运行账号与迁移账号分离，凭据不输出；结束均停止实例。未接触现有业务库。

### 命令与结果

| 实际命令原文 | 实际结果 |
| --- | --- |
| uv run --frozen --no-sync python scripts/test_presales_postgres.py --pg-bin 'C:/Program Files/PostgreSQL/18/bin' --pytest-args -q | 开工基线：75 passed，0 failed，1 skipped；24.40 秒 |
| uv lock --offline | 通过；255 packages resolved；不安装依赖 |
| uv run --frozen --no-sync python scripts/test_presales_postgres.py --pg-bin 'C:/Program Files/PostgreSQL/18/bin' --check-vector --pytest-args -q -rs | 中间专项：121 passed，0 failed，1 skipped；95.17 秒；随后增加恢复与清单测试，不用此数冒充最终全量 |
| uv run --frozen --no-sync python scripts/test_presales_postgres.py --pg-bin 'C:/Program Files/PostgreSQL/18/bin' --check-vector --full-suite --pytest-args -q -rs | 最终全量：318 passed，0 failed，5 skipped，18 warnings；122.43 秒；数据库已停止 |
| uv run --frozen --no-sync pytest tests/presales -q -rs | 默认无数据库配置：72 passed，0 failed，55 skipped，5 warnings；13.07 秒；54 个专用数据库用例按既有 opt-in 规则跳过，另 1 个 OS 符号链接用例跳过；这些数据库用例已在前述真实数据库全量运行中执行 |
| uv run --frozen --no-sync ruff check | 通过；0 lint 错误 |
| uv run --frozen --no-sync ruff format --check | 失败；10 个任务外既有文件需要格式化，未修改它们 |
| uv run --frozen --no-sync ruff format --check @targets | 通过；本任务 28 个 Python 文件已格式化；targets 为前述新增生产/测试 Python 文件、0004 迁移及测试脚本 |
| uv run --frozen --no-sync pyrefly check | 通过；0 errors，11 suppressed，4 warnings not shown；未改变既有抑制配置 |
| git diff --check | 失败；仅开工前 contracts.py 第 16、37 行尾随空白；本任务不修复原修改 |
| uv run --frozen --no-sync pymarkdown scan docs/maintenance/phase2a1-delivery-2026-11-06.md | 通过；0 Markdown 问题；首次 MD014 文档命令块问题已修复并复验，未更改代码 |
| git diff --check -- pyproject.toml uv.lock scripts/test_presales_postgres.py | 通过；退出码 0；另对全部本任务 31 个文件逐行检查，无尾随空白 |

`@targets` 为 PowerShell 数组，实际定义为：

```powershell
Set-Variable -Name targets -Value @('src/presales/materials','src/presales/knowledge','src/presales/indexing','tests/presales/phase2a_helpers.py','tests/presales/test_parsing.py','tests/presales/test_material_manifest.py','tests/presales/test_knowledge_registry.py','tests/presales/test_knowledge_schema.py','tests/presales/test_knowledge_review.py','tests/presales/test_knowledge_publish.py','tests/presales/test_index_build.py','tests/presales/test_phase2a_boundaries.py','tests/presales/test_knowledge_capacity.py','tests/presales/test_embedding_adapter.py','tests/presales/test_phase2a_recovery.py','migrations/versions/0004_knowledge.py','scripts/test_presales_postgres.py')
```

格式检查失败的 10 个既有文件：src/memory/postgres.py、src/presales/access/jwt.py、src/presales/access/policy.py、src/presales/application/auth.py、src/presales/application/service.py、src/presales/domain/contracts.py、src/presales/infrastructure/config.py、src/presales/infrastructure/database.py、src/presales/maintenance/service.py、src/run_service.py。

全量 5 个跳过：两个 tests/integration/test_docker_e2e.py、两个 tests/smoke/test_persistence.py 均需显式 --run-docker，本轮未执行容器验证；tests/presales/test_units.py 的符号链接创建因 OS 权限跳过，独立 Windows junction 用例仍执行。既有断言、跳过标记、默认测试配置及超时均未放宽。

首次新增回归曾出现 3 failed/101 passed/1 skipped；定位并修复了测试目录计数隔离与任务状态列权限问题。后续出现 1 failed/110 passed/1 skipped，撤权后残留 building 修复后重跑。失败样例和断言保留，没有以删除用例或换模式制造通过。

沙箱内 uv 缓存多次因权限拒绝无法启动；随后通过环境审批在沙箱外执行同一离线环境。该失败不计作 pytest 通过或失败用例。

### 未执行项与退出证据映射

| 未执行/尚未实现 | 对应证据或边界 | 原因 |
| --- | --- | --- |
| 真实 Embedding/模型/Reranker/Judge 调用与 Hit@8/Recall@8 | 设计第 8 节真实语义质量证据 | 用户禁止外发与真实模型；Fake 仅证明流程、契约、权限和恢复 |
| Docker/Linux 镜像及跨平台扩展兼容 | 第 9 节部署兼容待验证项 | 本轮只授权本机真实数据库；未启动容器、未安装新依赖 |
| API/MCP 两路检索、缓存失效、evidence 读取 | 2A-2 的退出证据、AC04/05/06/AC16 检索部分/AC22 完整路径 | 不属于 2A-1；当前只交付发布目录门禁和目录修订，无缓存或检索器 |
| React 材料/知识审核页面、业务 HTTP 集成 | 2A-3/UI 退出证据 | 本任务仅应用服务与持久化，不把 Python 测试当浏览器验收 |
| 固定方案影响提示、提交/批准证据复核、历史批准失效提示 | 阶段三 AC16 剩余证据 | 本阶段只保存知识变化事件，不改写历史方案 |

任务 3～6 所要求的工程能力已实现；不将上述后续阶段或未执行的真实质量/部署验证写作通过。

## 工作区与后续建议

开工前已存在的 tracked 修改：AGENTS.md、README.md、STATUS.md、docs/AGUI.md、docs/presales-requirements-decisions.md、工作流与发布恢复设计、总体技术设计、总体架构总览、阶段二A设计、src/presales/domain/contracts.py、src/presales/maintenance/service.py，以及删除的 docs/开发分支管理教程.md；另有未跟踪的双入口设计文档和 frontend/。首轮 git diff --stat 为 12 files changed、97 insertions、421 deletions；这些均排除本任务 diff 报告。

所有写入与格式化命令限定为本报告文件清单；没有清理工作区、修改需求/设计/STATUS、commit 或 push。最终复核上述 12 个既有 tracked 文件的 diff 统计仍为 97 insertions、421 deletions，与开工一致。开工临时哈希快照位于隔离临时目录，后续调用未能重新读取，因此不宣称逐字节哈希复核；依据开工状态/统计与明确写入范围核对。本任务共 31 个文件，其中 28 个 Python 文件；逐行空白检查无问题，任务范围内 tracked diff --check 退出码 0。

建议负责人据本报告复核后，将 2A-1 标注为工程实施与本机数据库专项验证完成；这只是建议，未修改 STATUS.md。继续 2A-2 时必须复用当前授权、发布目录/范围冲突门禁及目录修订，补齐真实检索与缓存反例。真实语义能力和 Linux 部署需独立运行、独立报告。
