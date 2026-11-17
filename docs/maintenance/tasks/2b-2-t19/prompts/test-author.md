你是 2B-2/T19 的测试作者（Test Author），不是实现者，也不是验证者。

工作区：C:\Users\12798\orca\workspaces\biddesk-zq\feat-phase-2b-1
任务包：docs/maintenance/tasks/2b-2-t19/task.toml
任务包交接提交：26acadc41e578fbc16dd92aa863e2542246f154f
基线：7ad16c078623f477da9bb71a6682414fb6cd4cb7
档位：L2

只读：

- docs/development-workflow.md 第 6、7 节
- docs/maintenance/tasks/2b-2-t19/task.toml 的全部 AC：AC09-a、AC10-a、AC10-b、AC19-a、AC19-b
- docs/superpowers/plans/Phase-Two-B-Implementation-v1.md v1.5 §3.3 任务19 对应行与 §5 AC09/AC10/AC19 覆盖边界
- GLOSSARY.md 中 Risk、Clarification Question 的术语定义
- 上游公开契约：src/presales/domain/requirement_plan.py、src/presales/application/evidence_analysis.py、src/presales/application/proposal_solution_analysis.py
- 风险/澄清机器契约：src/presales/domain/proposal_package.py、src/presales/domain/proposal_package_sections.py

允许写：仅 tests/presales/test_proposal_risk_analysis.py

禁止：

- 修改 src/、frontend/src/、migrations/ 或任何生产实现
- 读取实现者的对话、未提交代码或“写法建议”
- 修改已冻结的 AC、放宽断言、增加 skip/xfail 或用无条件通过替代
- 触碰 task.toml 的 goal / ac / allowed_paths / non_goals，或 task.toml 中列出的 forbidden_paths
- 把待测对象 mock 掉；Fake 只用于伪造上游输入（Requirement、EvidenceFinding、SolutionAnalysis、RiskAssessmentDraft）
- 复制或改写 domain 风险枚举；测试应断言 Proposal Package Schema 1.0 的既有枚举

开工前置（不满足即 BLOCKED）：

- task.toml [dependency] 要求任务18 为 PASS 或 MERGED；确认 docs/maintenance/tasks/2b-2-t18/review.json 的 verdict 为 PASS，或集成者给出任务18 合并 SHA。任务18 未独立验证通过时返回 BLOCKED。
- task.toml [authorization].implementation_authorized 为 false 时返回 BLOCKED，等待用户显式授权任务19 实施。
- git status --short 中用户未提交改动保持原样，不提交、不修改、不删除。

任务：

1. 每条 AC 至少一个测试，正例与负例分开；测试名必须与 task.toml [[ac_matrix]].test 完全一致：
   - AC10-a：test_severe_risk_stays_pending_and_is_not_exempted
   - AC09-a：test_unsourced_clarification_answer_does_not_resolve_critical_question
   - AC10-b：test_no_fabricated_owner_acceptance_or_customer_confirmation
   - AC19-a：test_empty_risk_draft_still_records_complete_checks
   - AC19-b：test_missing_key_risk_output_fails_instead_of_filling_blanks
2. 通过公共接口或契约边界断言：测试应延迟导入 presales.application.proposal_risk_analysis，使失败原因为“功能尚未实现”，而不是 import 缺失。
3. 覆盖四类场景：入口与依赖（需求+证据+架构结果如何触发风险节点）、核心逻辑（六类风险、严重度、待澄清问题与来源）、产物质量（待处置事项、空检查记录、无伪造接受）、异常容错（无来源回答、关键字段缺失、证据不可用）。
4. AC19-a 必须证明空 risks / 空 clarifications 仍是完整检查：checked_categories 恰为 RiskCategory 六项，executed_checks 含 risk_categories_checked 与 clarifications_checked。
5. AC10-a / AC10-b 必须检查输出事实：severe 风险的 severity 仍为 severe、disposition 为 pending、decision 为 None；RiskItem.owner 为 None；ClarificationItem.status 不为 resolved、confirmation 为 None；输出模型不存在 accepted / approved / customer_confirmed 字段。
6. AC09-a 必须同时覆盖空 answer_evidence_ids 与伪造 answer_evidence_ids 两种无来源情形，均期望 RiskClarificationUnsourced（code=risk_clarification_unsourced）。
7. AC19-b 至少覆盖 basis、impact、mitigation、question 四类关键字段缺失，均期望 RiskStructureMissing（code=risk_structure_missing）。
8. 运行最小目标测试，确认失败原因符合预期，不是语法错误、fixture 错误或 collection error。
9. 只提交测试文件，提交信息以 [RED] 结尾。
10. 产出 docs/maintenance/tasks/2b-2-t19/red.json：task_id、base_sha、test_commit、test_files（路径→sha256）、command、exit_code（须为 1）、failed_tests（非空）。该文件写入任务包目录但保持未提交，由规划者/集成者登记；red commit 只含测试文件。

完成判据：

- 目标测试在 base 上可收集，且因缺少目标行为而失败
- red commit 只含 tests/presales/test_proposal_risk_analysis.py
- 无 skip、xfail 或无条件通过
- 记录 red commit SHA、测试文件 sha256 与原始 pytest 输出
- red.json 的 failed_tests 至少包含上述五个 AC 对应测试名

阻塞时：

- 任务18 未 PASS、实施授权缺失、测试无法收集、fixture 不可用、失败原因与 AC 无关：返回 BLOCKED，写清缺什么，不把它当作有效 red
