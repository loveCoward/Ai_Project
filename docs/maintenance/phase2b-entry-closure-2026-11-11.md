# 二B任务1进入条件闭环记录

日期：2026-11-11（Asia/Shanghai）。当前结论：**D0—D4外部进入条件已满足；D5/D7/D8继续作为二B内部成果门禁，D6在相应真实任务执行时复核。任务1已完成，可按既有2B-0实施范围继续，不新增重复授权。**

本记录取代[任务1审查快照](phase2b-entry-review-2026-11-11.md)和[首轮执行阻塞记录](phase2b-entry-followup-2026-11-11.md)中的旧进入结论；旧记录保留为当时事实，不覆盖当时失败。

本结论只关闭“进入二B的实施前置”，不表示二B代码已实现、PostgreSQL/PGVector专项全通过、P07能力验收或阶段验收完成。

## 1. D0与人工样例门禁

用户接受[二B设计](../superpowers/specs/阶段二B-Agent分析工作流设计方案.md)指纹`7128DE782BAA77250952CADB7D64A1600C927579C32995BF5265F6039B6135D6`作为2B-0实施依据，原2B-0实施范围授权继续有效。接受和单轮D3调用范围由[接受记录](phase2b-entry-acceptance-2026-11-11.json)在补证执行前落盘，落款按用户提供的`<名字>`原样保存。

用户同时认可[样例包](phase2a-d3-sample-review-2026-11-11.md)的10例输入与来源判断，性质为历史运行之后、本轮补证之前的人工事后复核。固定数据摘要保持`277865a4e47678fc9a110f2d48676b288995aea34665f4b43ad7771cfcb5939e`，未因接受或执行调整输入、标签或预期来源。

旧报告缺少历史执行前接受记录的事实不回填；时序由独立接受JSON和本轮执行回执表达。人工判断覆盖具体输入、预期来源、非预期返回的相关性期望及样例包声明的证据限制。新运行是否命中这些已接受来源，用固定摘要和来源正文进行确定性映射。

## 2. D3真实补证结果

用户将专用Embedding凭据配置到项目本地`.env`的`PRESALES_OUTBOUND_EMBEDDING__API_KEY`。配置值未进入报告、命令日志、JSON或本文；`.env`受`.gitignore`忽略。执行前后仅核对变量存在、已加载及值匹配，不展示秘密。

实际使用已有显式探针，执行一次，未改模型、端点、样例、标签、预算或失败规则：

```powershell
.venv/Scripts/python.exe scripts/test_presales_postgres.py --pg-bin 'C:/Program Files/PostgreSQL/18/bin' --check-vector --probe --probe-output docs/maintenance/phase2a-d3-rerun-credential-configured-2026-11-11.json
```

| 事实 | 实际结果 |
| --- | --- |
| 起止/进程 | `2026-11-11T12:41:07.941387+08:00`至`12:41:20.890197+08:00`；退出码0 |
| 数据库 | 临时回环PostgreSQL 18.6；PGVector 0.8.6及原生向量距离检查通过；结束后正常停止 |
| 模型 | `qwen3.7-text-embedding-flash`，1024维；预检响应身份/格式检查通过 |
| 调用预算 | 16次连续validated请求、844字符、模型调用累计7.461秒；最大批次4；分别低于20请求、12,000字符、600秒和20条批次限制 |
| 成本 | `unknown_no_amount_cap`；不可由预算反推已发生金额 |
| 检索结果 | 10例均`retrievable`、`missing_sources=[]`；13条已接受预期来源全部位于前3名，最差为DEV-10 product第3名 |
| 失败与降级 | Embedding失败标记为空；全部10例均保留`reranker_not_configured_rrf_retained`，真实精排未运行 |

逐例命中及稳定Evidence ID见[闭环证据JSON](phase2b-entry-closure-2026-11-11.json)；完整结果见[本次真实报告](phase2a-d3-rerun-credential-configured-2026-11-11.json)，真实起止、退出码及报告摘要见[执行回执](phase2a-d3-rerun-credential-configured-execution-2026-11-11.json)。

DEV-01至DEV-10的预期来源排名为：product/1；security/1；security/1、retention/2；retention/1、security/2；product/1；compatibility/1；risk/1；risk/1；compatibility/1；compatibility/1、product/3（斜线后为名次）。

该结果关闭架构第7.1节二A早期受限检索探针的任务1进入证据。它仍是基本链路观测，不证明：

- P07 Hit@8、Recall@8、Precision或其他冻结指标已通过；
- 相关来源足以回答所有细节，或无关干扰返回已降为零；
- 真实Reranker、冲突裁决、Agent分析质量或阶段五三轮评测已完成；
- 二A阶段验收、产品验收、部署批准或二B整体完成。

## 3. 最新D0—D8判断

| 代号 | 当前判断 |
| --- | --- |
| D0 | 满足：指定设计获接受为2B-0依据；既有实施范围保留 |
| D1 | 任务1所需历史工程交付证据已核对并采信；本次未重跑全套检索测试 |
| D2 | 任务1所需页面、认证、到期、撤权和真实API历史证据已核对；浏览器限制保留 |
| D3 | 满足任务1早期探针进入证据：人工复核先于新运行，真实补证成功并映射到已接受来源 |
| D4 | 满足基础核对：来源闭包、命令回执与修订能力可复用；Attempt集成仍待任务实现 |
| D5 | 二B内部任务5/6成果；完成前不得宣称受控续跑的提交边界fencing可用 |
| D6 | 本轮临时PostgreSQL/PGVector启动和回收再次成功；全套售前测试、角色权限和saver提交边界仍须对应真实任务验证 |
| D7 | 二B内部任务25/26成果；本轮二A检索样例接受不替代二B分析模型/数据/调用计划 |
| D8 | 二B内部任务31/32成果；不误列为当前二A缺失前置 |

因此任务1的审查结论更新为**外部进入条件满足，可进入既有2B-0授权范围**。D5/D7/D8按其产出任务推进，不再错误地反向阻塞任务1；任务2起仍须逐项满足自身实现、测试和真实执行门禁。

## 4. 工程质量与验证边界

本轮仅在已有runner上增加`--probe-output`，要求显式`--probe`、新文件路径及已存在父目录，非法输入在数据库初始化前拒绝；默认入口保持兼容。新增CLI负例测试，验证单参数、已有报告和缺失父目录三种拒绝，且不启动数据库或子进程、不覆盖旧报告。

| 检查 | 结果 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_phase2a_tools.py tests/presales/test_probe_output.py -q -rs` | 15通过、0失败、0跳过 |
| `.venv/Scripts/ruff.exe check scripts/test_presales_postgres.py tests/presales/test_probe_output.py` | 通过 |
| `.venv/Scripts/ruff.exe format --check scripts/test_presales_postgres.py tests/presales/test_probe_output.py` | 通过，2文件已格式化 |
| Markdown扫描、JSON解析与工件断言 | 完成后作为本轮收尾执行；结果见最终任务回复，不预先宣称通过 |

本次未改共享业务模块或前端，未运行全量pytest、前端typecheck/build、浏览器E2E、真实Reranker或二B分析模型。未知金额不补造，旧报告、0请求阻塞报告及本次成功报告分别保留。未记录凭据、未提交Git、未部署。
