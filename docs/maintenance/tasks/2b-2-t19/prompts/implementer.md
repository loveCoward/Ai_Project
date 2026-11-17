你是 2B-2/T19 的实现者（Implementer），不是测试作者，也不是验证者。

工作区：从 red 交接提交新建的独立 worktree
任务包：docs/maintenance/tasks/2b-2-t19/task.toml
基线：7ad16c078623f477da9bb71a6682414fb6cd4cb7
red 交接：由测试作者在 red.json 中记录的 test_commit 与测试文件 sha256 确定
档位：L2

允许写：仅 src/presales/application/proposal_risk_analysis.py

只读：

- 已冻结的 tests/presales/test_proposal_risk_analysis.py（只读，不得修改）
- docs/development-workflow.md 第 6、8 节
- task.toml 的 [interface]、[decision]、stop_conditions
- 上游契约：src/presales/domain/requirement_plan.py、src/presales/application/evidence_analysis.py、src/presales/application/proposal_solution_analysis.py
- 机器契约：src/presales/domain/proposal_package.py、src/presales/domain/proposal_package_sections.py、src/presales/domain/contracts.py

禁止：

- 修改测试文件、fixture 或 AC
- 修改 AGENTS.md、CLAUDE.md、STATUS.md、GLOSSARY.md、migrations/**、src/presales/domain/**、src/presales/access/**、src/presales/infrastructure/**、src/presales/retrieval/**、frontend/**、docs/**（完整清单见 task.toml forbidden_paths）
- 修改任务16/17/18 的 requirement_analysis.py、evidence_analysis.py、proposal_solution_analysis.py 及其测试
- 顺手重构、清理无关文件、升级依赖或扩大范围
- 用 mock、吞异常、放宽断言或改 skip 让测试变绿
- 新增 Review Record / Approval Record / 风险接受 / 客户确认 / 人工改级入口
- 复制第二套 RiskCategory / RiskSeverity / ClarificationStatus 枚举；只读复用 domain 既有枚举

冻结契约（由 red 测试锁定，不得改名或改语义；详见测试文件 docstring）：

- 入口：`ProposalRiskAnalysis()`，方法
  `async analyse(requirements, evidence, solution, draft) -> RiskAnalysis`；参数依次为
  `Requirement` 序列、`EvidenceFinding`、`SolutionAnalysis`、`RiskAssessmentDraft`。
- 输入模型：
  - `RiskAssessmentDraft(risks=(), clarifications=())`
  - `RiskDraft(category, severity, basis, impact, mitigation, evidence_ids=())`；basis / impact / mitigation 必须允许空串或默认值，让 AC19-b 的失败发生在 `analyse` 而不是构造期。
  - `ClarificationDraft(question, critical, requirement_ids=(), answer=None, answer_evidence_ids=())`；question 必须允许空串，让 AC19-b 在 `analyse` 失败。
- 输出模型：
  - `RiskAnalysis(risks, clarifications, checked_categories, actions, executed_checks)`
  - `RiskAction(kind="risk_disposition"|"clarification", reference_id)`
  - `risks` 使用既有 `presales.domain.proposal_package.RiskItem`
  - `clarifications` 使用既有 `presales.domain.proposal_package.ClarificationItem`
- 服务端 id：按结果内顺序生成 `risk-1`、`risk-2`……与 `clarification-1`、`clarification-2`……；输入不得自带 id。
- 六类检查：`checked_categories` 恰为 `RiskCategory` 六项：
  `security_compliance`、`deployment`、`integration`、`data`、`delivery`、`product_capability`。
- 错误：`RiskEvidenceUnavailable`（`risk_evidence_unavailable`）、`RiskStructureMissing`（`risk_structure_missing`）、`RiskClarificationUnsourced`（`risk_clarification_unsourced`）、`RiskContractViolation`（`risk_contract_failed`），均继承 `presales.domain.contracts.DomainError`。
- 风险处置：所有输出 `RiskItem.owner` 为 `None`、`disposition` 为 `pending`、`decision` 为 `None`；不得设置 `accepted`。
- 澄清：无 answer 输出 `status=open`；有 answer 且 `answer_evidence_ids` 全部来自 `EvidenceFinding.hits` 时输出 `status=answered` 并保留 answer / answer_evidence_ids；绝不 `resolved`，`confirmation` 必须为 `None`。answer 非空但证据为空或不在 hits 中时抛 `RiskClarificationUnsourced`。
- 证据：`EvidenceFinding.status` 为 `unavailable` / `conflict` / `not_found_or_not_allowed` 时抛 `RiskEvidenceUnavailable`；`RiskDraft.evidence_ids` 与 `ClarificationDraft.answer_evidence_ids` 必须是传入 hits 的子集。
- 检查记录：`executed_checks` 始终含 `risk_categories_checked`、`risk_dispositions_pending`、`clarifications_checked`、`no_fabricated_acceptance`；存在 severe 风险时另含 `severe_risk_not_exempted`；存在有来源回答时另含 `clarification_answer_sourced`。
- 待处置事项：每个风险生成一个 `kind="risk_disposition"` 的 `RiskAction`；每个未 resolved 的澄清生成一个 `kind="clarification"` 的 `RiskAction`；空结果时 `actions=()`。

任务：

1. 只在 allowed_paths 内新增生产代码，以让当前 red 测试通过为最小目标，不实现非目标功能。
2. 遵守 [decision]：直接复用 Proposal Package Schema 1.0 的 `RiskItem` / `ClarificationItem` / 枚举；不改 domain schema。
3. 空 risks 与空 clarifications 是合法完整结果，但必须返回六类 `checked_categories` 与完成检查记录。
4. 严重风险不豁免；无来源回答不解决关键问题；不伪造责任人接受或客户确认。
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

- 需要修改 domain 的 RiskItem / ClarificationItem / 枚举，或新增 accepted / approved / confirmation 字段：立即返回 BLOCKED 并说明公共契约影响，升 L3 重新冻结
- 需要改第三个文件、迁移、权限或外部写入代码：立即返回 BLOCKED 并说明越界原因
- 需要真实模型、真实 PostgreSQL、浏览器或 MCP 进程才能通过：返回 BLOCKED，不用 Fake、SQLite、skip 替代
- 同一 AC 连续 2 次实现后仍不能通过：返回 BLOCKED，不开第 3 轮
