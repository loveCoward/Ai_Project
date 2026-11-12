# 2B-2任务17交付记录：Evidence与Published Knowledge Version专业分析

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务17的证据节点端口契约验证（A 层）与一次本机临时 PostgreSQL 复验（B 层，因缺陷修复触及共享检索模块），不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。未调用真实模型、未接真实CRM、无外部写入。

## 1. 实际交付范围

- `src/presales/application/evidence_analysis.py`：`EvidenceAnalysis` 通过只读端口（`RetrievalPort`/`EvidenceReader`/`CatalogPort`/`MaterialPort`）取得证据，**不实现**任何检索、排序、缓存或权限逻辑；`EvidenceHit` 逐条保留 2A 返回的证据ID、来源版本、定位、声明与适用性；`EvidenceFinding` 以 `content/empty/degraded/not_found_or_not_allowed/conflict/unavailable` 六种结局与 `checked` 检查清单表达结果。
- 降级透传：`RetrievalResult.degradation` 原样保留（如 `reranker_failed_rrf_retained`、`embedding_failed_keyword_only`），本层不新增、不改写、不吞掉降级标记。
- 目录与材料：目录（Product Catalog）只在显式启用时调用，结果单独标记 `origin="catalog"`；目录失败（含端口抛异常）降级为 `catalog_unavailable`/`catalog_not_allowed`，不影响已取得的检索证据。任务材料按 `Workbench.inputs` 真实形状（`complete`/`unresolved`）判定，未就绪标记降级 `task_material_not_ready`；无权/技术不可用时给显式结局而不外溢异常。**仍未读取材料正文**，正文读取归任务23。
- `verify()`：对每条 `knowledge` 证据按 ID 重读，重读失败统一降级为 `not_found_or_not_allowed` + `evidence_unavailable`，来源版本被替换则抛 `evidence_contract_failed`，绝不让失效引用继续呈现为 `content`。
- `evidence_sources()`：按来源版本去重输出，供任务15 的 Evidence Snapshot 登记使用。

未实现：架构/风险/汇总节点、Reflection、落稿、图装配、HTTP/SSE、前端页面；不重写 BM25/RRF/向量库；不做召回率或语义质量结论。

## 2. 可判定的行为

结局互斥：`content`（有证据）、`empty`（成功但无证据，`error_code=None`）、`degraded`（无证据但有降级原因）、`not_found_or_not_allowed`（无权/不存在）、`conflict`（目录冲突 `revision_or_request_conflict`）、`unavailable`（`not_ready` 等技术不可用）。

来源保真：证据的来源版本、定位（`pdf_page` 等）、声明与适用性原样保留；空白正文被判为 `evidence_contract_failed`，不当作有效证据。

越权防护：业务层不接触数据库与授权，只有 2A 端口能返回证据；本层不对端口返回值做任何放宽。Fake 端口证明「调用次数与参数」符合预期，不证明真实召回。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC09 证据可定位且版本保留 | 正例：ID、来源版本、定位、声明、适用性逐项断言 | `test_evidence_keeps_version_locator_and_applicability` |
| AC09 失败与空结果区分 | 正例：空结果为 `empty` 且无错误码；无权为 `not_found_or_not_allowed` | `test_empty_result_is_distinct_from_failure` |
| AC09 技术不可用与冲突分离 | 反例：`NotReady`→`unavailable`+`not_ready`；`Conflict`→`conflict`+`revision_or_request_conflict` | `test_not_ready_and_conflict_are_reported_apart` |
| AC19 精排超时可见退 RRF | 正例：`reranker_failed_rrf_retained` 原样透传，`status` 仍为 `content` | `test_degradation_is_passed_through_verbatim` |
| AC19 降级不等于成功有证据 | 反例：仅有 `embedding_failed_keyword_only` 时为 `degraded`、`hits` 为空 | `test_degraded_only_result_is_not_reported_as_content` |
| AC04/05 目录结果与来源隔离 | 正例：目录命中标记 `origin="catalog"`，并记录 `published_catalog_scope` 检查 | `test_catalog_output_is_labelled_and_isolated` |
| AC19 目录失败不阻塞主链 | 反例：`unavailable`/`not_found_or_not_allowed` 转为降级标记 | `test_catalog_failure_degrades_instead_of_raising` |
| AC16 未启用即不调用目录 | 反例：`use_catalog=False` 时目录零调用且无对应检查项 | `test_catalog_is_not_consulted_when_disabled` |
| AC09 材料未就绪可见 | 反例：不可访问材料 → `task_material_not_ready` | `test_unresolved_task_material_is_marked_degraded` |
| AC09 两路结果不互相覆盖 | 正例：同一证据来自两端口时分别保留两个来源标记 | `test_duplicate_hits_from_both_ports_are_collapsed` |
| AC09 证据缺失不虚构 | 反例：空白正文 → `evidence_contract_failed` | `test_blank_evidence_text_is_a_contract_violation` |
| AC04/05 引用失效不可继续当有效 | 反例：重读失败 → `not_found_or_not_allowed`+`evidence_unavailable`；正例：可读时仍为 `content` | `test_verify_rereads_every_citation_and_fails_when_unreadable` |
| AC09 版本保护 | 反例：重读返回不同来源版本 → `evidence_contract_failed` | `test_verify_detects_a_source_version_swap` |
| AC19 失败结局不可下游消费 | 反例：`require_usable()` 对拒绝结局抛 `evidence_node_failed`；正例：可用结局自返回 | `test_finding_requires_usable_status_before_downstream_use` |
| AC13 供快照登记的来源清单 | 正例：同一版本只出现一次并带来源类型 | `test_evidence_sources_lists_each_version_once` |
| AC19 查询身份可追溯 | 正例：`query_digest` 与请求 `digest` 一致 | `test_query_digest_is_stable_and_matches_the_request` |
| AC04/05 材料越权可判定且不泄漏 | 反例：材料端口 `AccessDenied` → `not_found_or_not_allowed` + `task_material_not_found_or_not_allowed`，`hits` 为空 | `test_task_material_denial_is_reported_without_leaking_text` |
| AC19 材料技术不可用显式结局 | 反例：材料端口 `NotReady` → `unavailable` + `task_material_not_ready` | `test_task_material_not_ready_is_reported_as_unavailable` |
| AC09 材料缺口显式降级 | 反例：真实 `Workbench.inputs` 形状（`complete=false`/`unresolved`）→ `task_material_not_ready`；正例：`complete=true` 时不降级 | `test_unresolved_task_material_is_marked_degraded`、`test_resolved_task_material_does_not_degrade` |
| AC09 兼容旧材料清单形状 | 正例：`sources/accessible` 旧形状仍判为未就绪 | `test_legacy_accessible_flag_payload_is_still_honoured` |
| AC09 未识别材料负载不静默放行 | 反例：既非 `complete/unresolved` 也非 `sources/required` → `evidence_contract_failed` | `test_unrecognised_material_payload_is_a_contract_violation` |
| AC16 无 task_id 不调用材料端口 | 反例：零调用且无 `task_material_manifest` 检查项 | `test_materials_are_not_consulted_without_a_task` |
| AC04/05 目录端口异常不外逸 | 反例：目录端口抛 `AccessDenied` → 降级为 `catalog_not_allowed`，检索证据仍保留 | `test_catalog_exception_degrades_instead_of_escaping` |
| AC09 目录成功但无命中不算失败 | 正例：`status="ok"` 且无命中 → `empty`、无错误码、无降级 | `test_successful_but_empty_catalog_is_not_a_failure` |
| AC04/05 端口参数原样透传 | 正例：检索端口收到的 actor 与查询文本与请求一致 | `test_retrieval_port_receives_the_actor_and_query_verbatim` |
| AC22 非法声明被拒 | 反例：`real_customer_sensitive` 声明 → 校验错误 `permitted source declaration` | `test_query_rejects_unpermitted_declaration` |
| AC04 来源范围不可拓宽 | 反例：`source_scope` 改为 `opportunity` → 校验错误 | `test_query_cannot_widen_the_source_scope` |
| AC09 端口返回集合外声明被拒 | 反例：命中自带 `real_customer_sensitive` → `evidence_contract_failed` | `test_unpermitted_source_declaration_from_the_port_is_a_contract_violation` |
| AC19 端口返回超界正文被拒 | 反例：正文超 20000 字符 → `evidence_contract_failed`，不静默截断 | `test_oversized_evidence_text_from_the_port_is_a_contract_violation` |

未在本步验证（留到后续步骤）：架构/风险节点（任务18—19）；汇总与覆盖计算（任务20）；Reflection（21）；落稿（22）；图装配与真实链路（任务23—24）；真实检索召回、精排质量与语义正确性（任务26）；SSE 与运行页（任务27—30）。

### A层：离线工程验证

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest -q`（开工基线，本轮复核） | 541通过、0失败、111跳过 |
| `uv run --no-sync pytest tests/presales/test_evidence_analysis.py -q`（本轮收工） | 31通过、0跳过 |
| `uv run --no-sync pytest tests/presales -q`（本轮收工） | 355通过、107跳过、0失败 |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_recovery.py -q`（缺陷修复后） | 23通过、0失败 |
| `uv run --no-sync pytest tests/presales/test_phase2a_tools.py -q`（契约再生成后） | 12通过、0失败 |
| `uv run --no-sync pytest -q`（收工） | 554通过、0失败、111跳过（证据节点用例18→31条；既有时间漂移失败已修复；无新增失败、跳过或 xfail） |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check src tests` | 187 个文件均已格式化 |
| `uv run --no-sync pyrefly check` | 0错误 |

### B层：真实PostgreSQL

本步主体（证据节点）只做端口契约验证。因缺陷修复改动了共享检索模块 `retrieval/models.py` 与
`retrieval/service.py`，追加一次本机临时 PostgreSQL 复验（`scripts/test_presales_postgres.py`
自建回环实例，无共享环境、无容器）：

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --pytest-args -q -k "retrieval or evidence"`（本轮复核，PG 18.6 自建回环实例） | 75通过、394取消、0失败 |
| `uv run python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --full-suite --pytest-args -q`（本轮复核，耗时2分41秒） | 660通过、5跳过、0失败 |

5 条跳过为既有 opt-in 规则，不是本次新增。B 层验证的是「检索元数据回传在真实 PostgreSQL 上
可用」，不包含真实模型与语义质量结论。

### C层

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。

## 4. 未实现项、风险与后续任务

- 既有失败已修复（非本步引入）：`tests/presales/test_proposal_attempt_recovery.py::test_recorded_intent_lets_the_supervisor_resume_with_the_next_generation`。原因是测试用固定时钟 `2026-11-12 10:00 UTC` 生成租约，却与真实 `datetime.now(UTC)` 比较，真实时间越过租约时刻后必然失败。改为给 `supervisor()` 注入 `clock`，断言改为用注入时钟的上下界（`clock() < lease <= clock() + 30s`）判断，语义覆盖不变，但不再依赖系统墙钟。未删除用例、未放宽断言、未加 skip/xfail。
- 证据声明与适用性来源已修复：2A 的 `Evidence` 新增可选字段 `declaration`、`applicability`（`src/presales/retrieval/models.py`），`retrieve_evidence` 用重新校验后的当前版本元数据填充（`src/presales/retrieval/service.py`），`read_evidence` 同样回传版本元数据；节点侧改为优先采用命中自带的版本元数据，查询声明与适用性只作兜底（`src/presales/application/evidence_analysis.py`）。新增两条用例分别锁定「版本元数据优先」与「查询仅兜底」。
- 材料端口契约错位已修复：节点原先按 `sources/accessible` 读材料可访问性，而真实 `Workbench.inputs` 返回 `complete`/`unresolved`/`unused_sources`（`src/presales/application/workbench.py`）。现按真实形状判定并保留旧形状兼容，未识别负载直接抛 `evidence_contract_failed`，不再静默放行（`src/presales/application/evidence_analysis.py`）。
- 端口异常逃逸已修复：材料端口抛 `AccessDenied`/`NotReady`/其他 `DomainError` 时，原先会越过节点直接抛出；现分别收敛为 `not_found_or_not_allowed`+`task_material_not_found_or_not_allowed`、`unavailable`+`task_material_not_ready`、`conflict`/`unavailable`。目录端口抛异常同样降级为 `catalog_not_allowed`/`catalog_unavailable`，不影响已取得的检索证据。
- 契约同步：`Evidence` 新增字段后 `frontend/src/contracts.generated.ts` 过期，已用 `scripts/export_presales_dto.py` 重新生成（仅多出两个可选字段），`test_phase2a_tools.py` 恢复通过。
- 既有格式偏差已清理：按 `ruff format --check src tests` 检出 11 个 HEAD 上既不合规的文件（`src/memory/postgres.py`、`src/run_service.py`、`src/service/service.py`、`src/presales/access/jwt.py`、`src/presales/access/policy.py`、`src/presales/application/auth.py`、`src/presales/application/service.py`、`src/presales/domain/contracts.py`、`src/presales/infrastructure/config.py`、`src/presales/infrastructure/database.py`、`src/presales/maintenance/service.py`），已格式化；`git diff -w` 确认除空白与参数换行外无语义变化。
- 任务材料仅做「可访问性」降级标记，未读取材料正文。该缺口已写入实施计划 v1.4 任务23：图装配时按 Attempt 锁定版本经既有 `materials` 服务 `parsed_document` 取正文并形成 `origin="material"` 输入，未解析/未就绪/无权材料显式判待补充；真实库取证在任务24。本步不实现正文读取，也不以可访问性标记充当正文证据。
- 本层的 `Failure` 与降级只表达「2A 接口层结果」，不代表真实召回质量。D1 真实检索链路为 2026-11-11 历史证据，对应的是本轮修改前的 `retrieval/models.py`/`service.py`；v1.4 任务24 已登记「改动共享二A模块须在真实实例重跑 `-k "retrieval or evidence or materials"` 复验」，本轮仅跑了自建临时实例的检索/证据与全量 B 层（见第3节），不表述为 D1 能力复验通过。
