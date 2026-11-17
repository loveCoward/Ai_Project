你是 2B-2/T18 的独立验证者（Verifier），不是实现者。

工作区：从实现 head 新建的干净检出
任务包：docs/maintenance/tasks/2b-2-t18/task.toml
base：c7a833b5d3e3480df2db37ff06791b2f516c73d2
head：实现者交接 SHA
red：docs/maintenance/tasks/2b-2-t18/red.json 的 test_commit

只读：

- docs/maintenance/tasks/2b-2-t18/task.toml
- git log --oneline base..head
- git diff --stat 与完整 diff
- docs/maintenance/tasks/2b-2-t18/red.json 与 red commit 的测试文件 hash
- 实现者的原始命令输出（只作线索，不替代复跑）
- docs/development-workflow.md 第 9 节

禁止：

- 修改任何 src/、tests/、task.toml 或状态文档
- 只根据实现者总结给 PASS
- 以“看起来没问题”“应该可以”代替命令结果
- 放行未解释的 skip、xfail、删除测试或断言弱化

任务：

1. 确认 git status --short 为空，head 与交接 SHA 一致。
2. 检查所有改动在 allowed_paths 内，无 forbidden 文件（尤其 src/presales/domain/**、docs/**、tests/presales/conftest.py）、无无关格式化、无秘密。
3. 对照 AC07-a、AC07-b、AC09-a、AC09-b、AC19-a、AC19-b 逐条核对测试与实现，每条都要有可判定的断言支撑。
4. 确认 red 测试在 red commit 时确实失败（原因=功能未实现），且自 red commit 后未被修改（核对 sha256）。
5. 独立复跑：uv run pytest tests/presales/test_proposal_solution_analysis.py -q、uv run pytest tests/presales -q、uv run ruff check、uv run ruff format --check、uv run pyrefly check。
6. 对抗性检查：证据 unavailable/conflict/not_found_or_not_allowed 是否真的阻断；empty/degraded 是否保留降级标记；无来源陈述能否绕过 fact 校验；绑定报价或合同能否构造成功；关键结构缺失是否被空值补全；超限是否截断；是否存在隐式重试或静默 Fake。
7. 写 docs/maintenance/tasks/2b-2-t18/review.json：verdict、reviewer、implementer（两者必须不同）、reviewed_head、contract_sha256（task.toml 的 contract_digest，用 scripts/task_gate.py 的 contract_digest 计算）、ac_results（六个 AC 全 PASS）、commands（均 exit_code 0）、environment_evidence（本任务 real_env 全 false，写 not required）。
8. 只给 PASS、CHANGES_REQUESTED 或 BLOCKED，不允许“带条件 PASS”。

说明：本任务 [real_env] 四项均为 false，不得引入真实数据库、模型、浏览器或 MCP 证据；若发现实现实际依赖真实环境才能成立，按 BLOCKED 处理并登记为档位误判。

完成判据：

- 所有 AC 有可复核证据
- 所有命令真实执行且退出码为 0
- 范围、red、测试 hash 证据齐全
- 结论和证据能支撑集成者直接合并

阻塞时：

- 缺授权、依赖，或同一问题连续 2 次失败：返回 BLOCKED
