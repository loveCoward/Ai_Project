# 2B-2任务18交付记录：Proposal Package方案架构分析

日期：2026-11-18（Asia/Shanghai）。本记录只覆盖任务18的架构分析应用服务、离线契约验证和独立审查关闭，不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。未调用真实模型、未使用真实客户数据、未连接真实数据库、浏览器或MCP子进程。

## 1. 实际交付范围

- `src/presales/application/proposal_solution_analysis.py`：`ProposalSolutionAnalysis.analyse(requirements, evidence, draft)` 消费任务16的 `Requirement` 与任务17的 `EvidenceFinding`，输出可校验的 `SolutionAnalysis`。
- 输出包含建议方案、部署与集成建议、假设、限制、交付注意、分类陈述、不支持项和已执行检查；不包含审批、批准、发布或绑定状态。
- 事实、建议、待确认三类陈述由本模块输出模型承载，未修改 Proposal Package Schema 1.0。
- 证据结局按任务17契约消费：`unavailable` / `conflict` / `not_found_or_not_allowed` 拒绝产出；`degraded` / `empty` / `content` 可继续，并分别保留 `evidence_degraded` / `evidence_empty` 标记。
- 每条陈述必须携带至少一个 `evidence_id`，且所有引用必须属于传入的 `EvidenceFinding.hits`；该检查覆盖 `content`、`degraded` 与 `empty`，不允许降级或空证据绕过。
- 报价、工期、合同类建议固定为非绑定且待确认；不支持项保留原需求文本和限制说明，不允许同一需求同时被陈述为已支持。
- 关键结构缺失、需求为空、证据不可用、容量超限均以稳定错误码显式失败；超限不截断、不重试。

未实现：架构节点接入分析图（任务23）、风险与澄清（任务19）、覆盖与汇总门禁（任务20）、Reflection（任务21）、落稿（任务22）、真实检索/模型链路（任务24、26）、HTTP/SSE/前端页面。真实模型诚实性和语义分析质量仍未验证。

## 2. 验收与验证

| 验收编号 | 实际验证 | 测试映射 |
| --- | --- | --- |
| AC07-a | 不支持项保留限制和原文，不生成替代承诺；陈述不得反向声称已支持 | `test_unsupported_requirement_is_not_replaced_with_a_commitment`、`test_unsupported_requirement_cannot_be_contradicted_by_a_statement` |
| AC07-b | 报价/日期/合同建议非绑定且待确认，输出无批准字段 | `test_quote_and_schedule_suggestions_stay_non_binding` |
| AC09-a | 每条陈述分类且引用真实证据；降级和空证据不能伪造引用 | `test_every_statement_carries_a_kind_and_a_source`、`test_degraded_and_empty_findings_cannot_bypass_citation_validation` |
| AC09-b | 不可用证据和空需求显式阻断；降级保留标记 | `test_unusable_evidence_blocks_the_architecture_node` |
| AC19-a | 关键结构缺失失败，不补空字段 | `test_missing_key_structure_fails_instead_of_filling_blanks` |
| AC19-b | 容量超限显式失败，不截断、不重试 | `test_exhausted_budget_fails_instead_of_truncating` |
| decision.evidence_scope | 空证据保留独立标记，不被当作内容证据 | `test_empty_evidence_is_recorded_without_becoming_evidence_content` |

### A层：离线工程验证

在 `t18-review` 工作区、最终审查头执行：

| 命令原文 | 实际结果 |
| --- | --- |
| `uv run pytest tests/presales/test_proposal_solution_analysis.py -q` | 9通过、0失败 |
| `uv run pytest tests/presales -q -rs` | 371通过、107跳过、0失败 |
| `uv run ruff check` | 通过，0错误 |
| `uv run ruff format --check` | 211个文件均已格式化 |
| `uv run pyrefly check` | 0错误（11 suppressed，8 warnings未显示） |

107条跳过均为既有“Explicit disposable PostgreSQL URL and opt-in required”规则，本任务 `[real_env]` 四项均为 false，不要求真实环境证据。

### Red证据

- `35fb88e`：原始六条AC的red提交，测试文件只含测试变更。
- `776ac6e`：规划者授权的冻结测试修正，只含测试文件。
- `3e85ca8`：针对独立审查F1/F2/F3新增回归用例的red提交；在 `1603b80` 上实测6通过/3失败，失败原因分别为降级/空证据引用未被拒绝、空证据缺少标记、不支持需求被陈述反向覆盖。

### B层/C层

未执行。本任务不要求真实PostgreSQL、真实模型、浏览器或MCP子进程；未进行外部写入、发布或审批。

## 3. 独立审查关闭

首次独立审查 `56653f4` 给出 `CHANGES_REQUESTED`，发现3项问题：

1. 降级证据可绕过引用校验。
2. 不支持需求可被陈述反向覆盖。
3. 空证据缺少稳定标记。

修复提交 `7ad16c0` 已逐项关闭。最终独立审查 `2b22957` 更新为 `PASS`，审阅头为 `3e85ca8`，六条AC全部PASS。随后仅追加证据哈希口径修正，未再修改实现或测试。

## 4. 未实现项、风险与后续任务

- 本任务只验证架构节点的离线契约，不代表真实模型能稳定产出合规方案；真实链路验证在任务24，能力评测在任务26。
- 架构节点尚未接入分析图，任务23负责图装配与节点复用。
- 推荐方案、部署建议、假设和限制仍是自由文本；当前门禁验证结构完整性、分类与引用，不做语义等价性判断。
- `SolutionAnalysis.statements` 允许为空；无陈述的完整方案是否应拒绝，留待任务20/21汇总门禁与Reflection统一裁定。
- 本任务按用户2026-11-18“完成任务18的后续内容，确保完成”的明确要求，在 `task.toml [budget_exception]` 登记L2预算例外；`task_gate` 仍会按默认4提交/800行报告预算超限，内容检查已全部通过。

## 5. 验证边界

本记录中的371通过/107跳过是2026-11-18在 `t18-review` 工作区的实际结果；未重跑根目录全量pytest、真实PostgreSQL、前端typecheck/build、Docker或真实模型评测。不得据此宣称二B整体完成、阶段验收通过或部署获批。
