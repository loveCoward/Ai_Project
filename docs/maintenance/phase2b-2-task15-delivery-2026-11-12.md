# 2B-2任务15交付记录：Evidence Snapshot与Derived Artifact来源闭包

日期：2026-11-12（Asia/Shanghai）。本记录只覆盖任务15的离线契约与本机临时 PostgreSQL 专项验证，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。未调用真实模型、未接真实CRM、无外部写入。

## 1. 实际交付范围

- 迁移 `0011_evidence_snapshot`：新增 `presales.evidence_snapshots`（不可变节点产物与稳定执行身份唯一约束）、`presales.evidence_snapshot_sources`（逐条实际读取来源：类型、来源版本、证据ID、定位、内容 SHA-256、声明、适用性、检索运行）、`presales.evidence_snapshot_upstream`（产物直接上游边）与 `presales.node_readiness`（待就绪/失败账本）。迁移 head 由 `0010` 前进到 `0011`。
- 触发器和权限：快照内容除 `state` 外不可更新、不可删除；来源行不可删且必须指向真实 `resource_versions`；上游边插入前做递归环检测；`presales_runtime` 只有 `SELECT,INSERT` 与 `UPDATE(state)`，`presales_maintainer` 只读。
- `src/presales/domain/evidence_snapshot.py`：`NodeExecutionIdentity`（Attempt+节点+输入修订+代次+Reflection修订+技术重试）、`SourceReference`、`DerivedArtifact`、`ReconciliationStatus` 及 `evidence_contract_failed`/`cycle_detected`/`missing_source`/`not_ready` 错误码。
- `src/presales/infrastructure/evidence_snapshot_store.py`：短事务持久化；同身份重复提交返回既有产物而不新增权威行；同身份异内容冲突；按代次 fencing；闭包校验（来源版本必须 `ready` 且 `dependencies_complete`）；`link_upstream` 补登依赖并复用环检测。
- `src/presales/application/evidence_snapshot.py`：`EvidenceSnapshotService` 的 `save/load/reconcile/mark_pending/set_failed/link_upstream`；所有写入复用 `ProposalAttemptContext.boundary`，即当前架构师角色、任务与全部实际来源闭包、当前代次、`running`/`analyzing`、未取消的联合门禁。

未改 `src/presales/access/policy.py`：计划里列为「拟修改」，实现核对后确认 `Policy.source_closure()` 已满足需要，未改；这是相对开工说明第 4 块的偏离，已记入本记录第 4 节。

未实现：分析图、四专家链、Reflection、落稿、HTTP/SSE、前端页面、Evidence Snapshot 的对外查询接口；这些不在任务15范围内。

## 2. 可判定的行为

来源类型：材料、MCP背景、检索、Reflection 四类都以 `SourceReference.kind` 登记，并分别带定位、内容校验值、声明与适用性；同一版本在同一产物中重复登记被 `evidence_contract_failed` 拒绝。

闭包与环：上游必须是同一 Attempt 下已就绪的产物，否则 `missing_source`；插入会形成环的上游边被拒绝为 `cycle_detected`（真实库另有触发器 `evidence_snapshot_cycle_detected` 兜底）。

复用与对账：同一 `NodeExecutionIdentity` 二次保存返回同一 `artifact_id`，不新增权威行；同身份不同输出返回 `revision_or_request_conflict`；`reconcile` 在产物存在且闭包有效时返回 `reusable`，存在未结束调用登记时返回 `unknown` 并列出调用键，否则 `not_ready`。

代次与终态：过期代次写入返回 `fence_conflict`；已结束或已取消 Attempt 返回 `attempt_terminal`；两者都不落库。

授权收紧：来源版本当前无授权、或来源版本未就绪时，写入失败且不落库；`boundary` 会按现有约定把尝试标记为 `authorization_invalid` 终态，这是既有行为，不是本步新增。

引用删除不删实际依赖：快照的来源行和上游边独立于引用文本保存，删除引用不会级联删除它们；来源行本身禁止删除。

## 3. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC09 四类输入均登记 | 正例：material/mcp_background/retrieval/reflection 四类逐条登记并回读一致 | `test_each_source_kind_is_registered_with_traceable_closure` |
| AC04/05 来源按当前授权重建 | 反例：来源版本无当前授权 → `not_found_or_not_allowed` 且零落库 | `test_source_without_current_grant_is_denied_and_not_stored` |
| AC09 缺来源拒绝 | 反例：未登记版本 → `missing_source`；上游不存在 → `missing_source` | `test_unregistered_source_and_duplicate_source_are_rejected`、`test_cycle_and_missing_upstream_are_rejected` |
| AC09 环拒绝 | 反例：对已存在产物补登形成环的上游 → `cycle_detected`；真实库触发器同向拒绝 | `test_cycle_and_missing_upstream_are_rejected`、`test_cycle_is_rejected_by_the_database_guard` |
| AC13 同节点结果复用 | 正例：同身份二次保存同一 `artifact_id`、行数不增；反例：同身份异内容 → `revision_or_request_conflict` | `test_same_identity_reuses_result_without_second_authority_row`、`test_same_identity_with_different_output_is_a_conflict`、`test_service_persists_closure_and_reuses_identity` |
| AC13 旧代次不得激活 | 反例：`generation=1` 写入 `generation=2` 的 Attempt → `fence_conflict` 且零落库；离线另断言终态拒绝 | `test_stale_generation_is_refused_and_nothing_is_written`、`test_stale_generation_and_terminal_attempt_cannot_write` |
| AC16 内容不可改写 | 反例：runtime 改写快照 `body` → `storage_unavailable_reconcile_request`，原值不变 | `test_runtime_cannot_rewrite_snapshot_content` |
| AC09 来源必须真实 | 反例：指向不存在版本 → 数据库拒绝，零落库 | `test_snapshot_rows_are_immutable_and_sources_need_a_real_version` |
| AC13 待就绪/对账 | 正例：产物已提交 → `reusable`；反例：有未结束调用 → `unknown` 并给调用键；无产物无调用 → `not_ready`；`load` 无产物 → `not_ready` | `test_reconcile_reuses_committed_artifact`、`test_reconcile_keeps_unknown_when_a_call_is_open`、`test_reconcile_without_artifact_or_call_is_not_ready`、`test_load_missing_snapshot_is_not_ready` |
| AC13 失败账本 | 正例：`pending` 后可查；`failed` 携带 `error_code` | `test_readiness_ledger_tracks_pending_and_failure` |

未在本步验证（留到后续步骤）：真实并发与崩溃时序（任务24）；四专家产物 Schema 与完整性校验（任务23）；Reflection 前后版关联（任务21）；落稿事务与候选指针（任务22）；对外查询/SSE（任务27—30）；语义质量与真实模型（任务26）。

### A层：离线工程验证

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync pytest -q`（开工基线） | 480通过、106跳过 |
| `uv run --no-sync pytest tests/presales/test_evidence_snapshot.py -q` | 12通过、0跳过 |
| `uv run --no-sync pytest -q`（收工） | 492通过、111跳过、0失败（+12通过、+5跳过，跳过增量均为新增`@postgres`用例的既有opt-in规则） |
| `uv run --no-sync ruff check` | 通过，0错误 |
| `uv run --no-sync ruff format --check <改动文件>` | 通过 |
| `uv run --no-sync pyrefly check` | 0错误 |

### B层：真实PostgreSQL（本次已执行）

环境：本机 PostgreSQL 18.6 临时回环集群、PGVector 0.8.6、脚本自建自删；全合成数据；未连接共享库或客户数据；未使用容器。

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --pytest-args -q -rs -k "migration_upgrade_head_twice or evidence_snapshot"` | 17通过、0失败（含迁移重复升级至`0011`） |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin "C:/Program Files/PostgreSQL/18/bin" --check-vector --full-suite --pytest-args -q -rs` | 598通过、5跳过、0失败（154.06秒；跳过均为既有docker/symlink规则，与开工一致） |

### C层：本次未执行

未调用云端或本地真实模型，未使用真实客户数据，未向外部写入或发布。

## 4. 未实现项、风险与后续任务

- 相对开工说明第 4 块的偏离：计划中的 `src/presales/access/policy.py` 未修改，理由是既有 `source_closure()` 已提供所需授权闭包；其余文件均按清单交付。
- 测试文件拆成两个（`test_evidence_snapshot.py` 离线、`test_evidence_snapshot_postgres.py` 真库），比第 4 块多一个文件；原因是保持离线默认套件不依赖真实数据库，与仓库既有拆分方式一致。
- `node_readiness` 目前只记录节点级状态，未与 LangGraph checkpoint 身份绑定；该衔接留给任务23的图装配。
- `reconcile` 的「未结束调用」只按 `body->>'node'` 匹配调用登记；后续若调用登记格式变化，需要同步调整查询。
- 快照表只增不删，无归档策略；与执行记录一致，容量风险在二B退出评审登记（任务33）。
- 来源「可定位」不等于「论断受支持」；本步只证明依赖闭包完整性，不证明语义质量或引用恰当性。
