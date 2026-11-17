你是 2B-2/T18 的实现者（Implementer），不是测试作者，也不是验证者。

工作区：C:\Users\12798\orca\workspaces\biddesk-zq\t18-test（从 red 交接提交 cb934d4 继续）
任务包：docs/maintenance/tasks/2b-2-t18/task.toml
基线：c7a833b5d3e3480df2db37ff06791b2f516c73d2
red 交接：cb934d4（red 测试提交 35fb88e，测试文件 sha256 fa9b3481016d7fefbaa7dc6ec06b1393696ac77cb6bfeb5cbba23d1bb3384e1e）
档位：L2
允许修改：仅 src/presales/application/proposal_solution_analysis.py

只读：

- 已冻结的 tests/presales/test_proposal_solution_analysis.py（只读，不得修改）
- docs/development-workflow.md 第 6、8 节
- 上游契约：src/presales/domain/requirement_plan.py、src/presales/application/evidence_analysis.py
- task.toml 的 [interface]、[decision]、stop_conditions

禁止：

- 修改测试文件、fixture 或 AC
- 修改 AGENTS.md、CLAUDE.md、STATUS.md、migrations/**、src/presales/domain/**、src/presales/access/**、src/presales/infrastructure/**、src/presales/retrieval/**、frontend/**、docs/**（完整清单见 task.toml forbidden_paths）
- 顺手重构、清理无关文件、升级依赖或扩大范围
- 用 mock、吞异常、放宽断言或改 skip 让测试变绿
- 实现检索、排序、缓存或权限判定；这些由 2A 端口提供

冻结契约（由 red 测试锁定，不得改名或改语义；详见测试文件 docstring）：

- 入口：`ProposalSolutionAnalysis(max_characters=60_000)`，方法
  `async analyse(requirements, evidence, draft) -> SolutionAnalysis`；参数依次为
  `Requirement` 序列、`EvidenceFinding`、`SolutionDraft`。
- 模型：`SolutionDraft`（`recommended` / `deployment_integration` / `assumptions` /
  `limitations` / `delivery_notes` / `statements` / `commitments` / `unsupported`）、
  `SolutionStatement`（`text` / `kind` / `evidence_ids` / `requirement_ids`）、
  `CommitmentSuggestion`（`kind` / `text` / `requirement_ids` / `binding=False` /
  `confirmation_required=True`）、`UnsupportedRef`（`requirement_id` / `limitation`，输出需带
  原 `text`）、`SolutionAnalysis`（回显结构与 `executed_checks`）。
- 结构模型必须允许「不完整」实例化：`SolutionDraft.recommended` 需接受 `None` 与空白串，
  三个列表需接受 `()`，让 AC19-a 的失败发生在 `analyse` 而不是构造期。
- 错误：`SolutionEvidenceUnavailable` / `SolutionRequirementsEmpty` /
  `SolutionStructureMissing` / `SolutionCapacityExceeded` / `SolutionContractViolation`，
  code 分别 `solution_evidence_unavailable` / `solution_requirements_empty` /
  `solution_structure_missing` / `solution_capacity_exceeded` / `solution_contract_failed`，
  均继承 `presales.domain.contracts.DomainError`。
- `executed_checks` 至少包含 `unsupported_preserved`、`commitments_non_binding`、
  `statement_classification`，`EvidenceFinding.status == "degraded"` 时另含 `evidence_degraded`。
- 断言细节：`SolutionAnalysis` 不得有 `approval*` 或 `approved` 字段；总字符数超过
  `max_characters` 时在产出前抛容量错误，且不截断、不重试；`fact` 引用的 evidence id 必须
  出现在传入 `EvidenceFinding.hits` 中。

任务：

1. 只在 allowed_paths 内新增生产代码，以让当前 red 测试通过为最小目标，不实现非目标功能。
2. 遵守 [decision]：事实/建议/待确认 分类放在本模块的应用层输出模型，不改 Proposal Package Schema 1.0。
3. 证据按 EvidenceFinding 结局消费：empty/degraded 可继续但必须保留降级标记；unavailable/conflict/not_found_or_not_allowed 一律失败。
4. 承诺类输出保持非绑定且待确认；不输出已确认、已批准或已发布状态。
5. 不新增测试；如果发现测试本身有错，停止并报告，不自行改测试。
6. 运行：目标测试、uv run ruff check、uv run ruff format --check，再跑 uv run pytest tests/presales -q 确认无回归。
7. 提交实现，提交信息说明改了哪个行为，不混入格式化噪声。
8. 报告：文件清单、逐文件改了什么、受影响调用方、原始测试输出、未验证项。

完成判据：

- 目标测试由红转绿
- 未修改冻结测试；task_gate 的测试文件 hash 校验可通过
- 所有改动在 allowed_paths 内
- ruff check、ruff format --check、tests/presales 回归均通过

停止条件：

- 需要改第三个文件、公共契约（含 domain 的 SolutionBlock）、迁移或权限代码：立即返回 BLOCKED 并说明越界原因
- 同一 AC 连续 2 次实现后仍不能通过：返回 BLOCKED，不开第 3 轮
