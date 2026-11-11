# 2B-0续作交付与验证记录

日期：2026-11-11（Asia/Shanghai）。执行范围为实施清单任务1—6；用户本次明确批准B层PostgreSQL命令并要求连续执行至验证完成。**2B-0的契约、存储及checkpoint提交边界工程门禁已取得本次运行证据。** 本记录不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。

## 1. 续作依据及任务结果

进入前置沿用[任务1闭环记录](phase2b-entry-closure-2026-11-11.md)及其引用的接受记录、二A交付和真实检索探针；本轮未重新调用模型或Embedding。任务1的历史证据与本轮任务2—6测试结果分开记录。

| 任务 | 本轮核对与实际交付 | 结论 |
| --- | --- | --- |
| 1：进入条件 | 核对D0—D4闭环记录及工作区现码；D5由本轮任务5/6取证，D7/D8保留后续产出门禁 | 既有进入条件有效，未新增重复批准 |
| 2：Attempt契约 | 身份、执行阶段/结局、修订/代次、命令/恢复/投影DTO与错误码；补取消后不得重新运行、终态投影不得改写结局 | 契约专项通过；尚未接REST或监督器 |
| 3：Schema 1.0 | P06全部必需业务区块、引用关联、确定性覆盖、非绑定承诺、审批/发布/记忆写入未执行状态；旧手工草稿显式不完整；`proposal_package_json_schema()`由同一Pydantic定义生成机器Schema | 42项离线专项包含逐区块缺失负例；Schema合格不证明语义支持 |
| 4：持久记录 | Attempt、任务/全局槽位、投影与候选关联；调用/恢复/节点产物记录；checkpoint提交回执；最小权限、条件写入及不可变约束 | 0006显式迁移及真实角色/存储测试通过；未实现完整调用调度或恢复消费决策 |
| 5：checkpoint适配 | 每次checkpoint/pending writes在同连接事务内锁全局槽位、任务、任务槽位及Attempt并验代次/取消/终态；回执同事务持久化，内存回执只在提交后公布；拒绝外层未提交事务或已启用pipeline的saver | 锁定saver兼容及真实事务边界通过；不接入通用聊天checkpoint |
| 6：真实PG门禁 | head连续upgrade两次、角色隔离、两级排他、旧代次/取消拒写、暂停后失效、真实锁等待、blob/checkpoint/回执原子回滚、执行记录幂等与旧代次拒写 | 专项13通过；全量真实PG 418通过、5跳过 |

任务3原代码与测试超过800行，已先修订[实施清单](../superpowers/plans/Phase-Two-B-Implementation-v1.md)再按3a基础区块、3b聚合/兼容拆分，公共导入继续通过`proposal_package.py`。当前两切片分别381行、746行。任务4按4a持久Attempt/槽位、4b执行记录/提交回执分片；不改变任务1—33编号或后续阶段边界。最终迁移名为`0006_proposal_attempt.py`，前驱为已存在的二A `0005_retrieval`，没有覆盖二A迁移。

## 2. 环境与提交边界

本机Windows、Python 3.13，临时回环PostgreSQL 18.6，PGVector 0.8.6并执行原生向量距离检查。临时登录角色为`phaseadmin`、`phaseapp`、`phasemaint`，运行/维护组角色为`presales_runtime`、`presales_maintainer`。测试密码临时生成、输出脱敏、未写入本记录；数据库实例在每次运行结束后停止。

`uv.lock`与实际安装版本一致：LangGraph 1.2.11、langgraph-checkpoint 4.2.0、langgraph-checkpoint-postgres 3.1.2、psycopg 3.3.4。没有升级依赖。适配利用锁定saver的`_cursor()`入口覆盖checkpoint、blob和pending writes事务；类型签名沿用其RunnableConfig、Checkpoint、CheckpointMetadata与ChannelVersions定义。该内部接口若随依赖升级变化，必须重新跑本专项，不能沿用本报告结论。

专用checkpoint表在真实测试中由维护连接显式执行`AsyncPostgresSaver.setup()`并授予运行角色所需表权限；运行账号不执行DDL。当前没有部署初始化命令或生产权限配置的验证，不能把测试编排当作已部署。已启用pipeline、非autocommit或已有外层事务的连接明确拒绝，防止在外层尚未提交时生成“已提交”回执。

真实竞争用两个独立数据库连接和有界事件屏障：旧执行者进入写事务但尚未验代次时暂停，接管递增两级占位/Attempt代次或取消先提交，恢复旧写入后必须拒绝，且没有迟到checkpoint、blob、pending writes或回执。反向顺序在验代次持锁后暂停，观察`pg_stat_activity.wait_event_type='Lock'`证实取消等待；checkpoint与回执提交后取消才能提交。该测试证明数据库竞争与事务边界，不冒充任务12要求的完整跨进程生命周期/进程崩溃测试。

分别在checkpoint插入和回执插入处用测试触发器抛出异常，确认blob与回执一起回滚。调用/恢复/节点结果记录绑定稳定键、输入摘要与代次，重复同内容复用、异参冲突、终态结果不可改写；完整恢复意图领取及业务消费回执仍属任务11。

## 3. 本次验证结果

所有命令在仓库根目录执行，使用`uv run --no-sync`保留已锁定安装环境。以下数字均来自本次运行；不与历史次数拼接。

| 命令或检查 | 本次结果与证据 |
| --- | --- |
| `uv run --no-sync pytest tests/presales/test_proposal_attempt_contract.py tests/presales/test_proposal_package_schema.py tests/presales/test_proposal_package_sections.py tests/presales/test_proposal_attempt_storage_contract.py tests/presales/test_proposal_checkpoint_contract.py -q` | 42通过、0失败、0跳过；0.81秒 |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin 'C:/Program Files/PostgreSQL/18/bin' --check-vector --pytest-args -q -rs -k proposal_storage_postgres` | 13通过、214未选中、0失败、0跳过；10.98秒；[专项日志](phase2b-0-postgres-2026-11-11.log) |
| `uv run --no-sync python scripts/test_presales_postgres.py --pg-bin 'C:/Program Files/PostgreSQL/18/bin' --check-vector --full-suite --pytest-args -q -rs` | 418通过、5跳过、0失败；128.69秒；退出0，实例正常停止；[全量PG日志](phase2b-0-full-postgres-2026-11-11.log) |
| `uv run --no-sync pytest -q -rs`，无全局Fake开关 | 341通过、82跳过、0失败；29.27秒；[离线日志](phase2b-0-offline-clean-env-2026-11-11.log) |
| `uv run --no-sync ruff check`及`ruff format --check`，限定本轮14个Python文件 | 通过；14文件格式检查通过 |
| `uv run --no-sync pyrefly check` | 全项目0错误；11项既有抑制、6条未展示警告 |
| `uv run --no-sync pymarkdown scan docs/superpowers/plans/Phase-Two-B-Implementation-v1.md docs/maintenance/phase2b-0-delivery-2026-11-11.md` | 交付收尾实跑；最终结果见本次回复与执行回执 |

runner按用途固定pytest子进程上限：默认180秒，带`-k`的专项300秒，`--full-suite`全量900秒；离线测试验证分档。本轮全量实际128.69秒，未延长业务600秒主动预算。

全量PG的5个跳过分别为4个未启用Docker的集成/持久化冒烟用例，以及1个Windows系统未授予符号链接创建权限的用例。离线82个跳过包含77个需要显式临时PG授权与URL的专项，以及上述5个；数据库专项已在独立真实PG运行中执行，不能把离线跳过计为通过。全量18条警告来自既有LangGraph、langchain-community、Starlette/httpx和AG-UI弃用提示。

## 4. 失败记录与限制

续作时19项原契约及5项原PG测试先通过；进一步核对发现必需区块会静默补空，取消标记尚在排空时可重新运行，以及终态投影可改结局。新增负例首次产生9失败、19通过，修复后纳入上述专项与全量回归。初次pyrefly报7个saver重载签名错误，按锁定依赖类型修复后全项目0错误。

首轮离线全量由执行者设置`USE_FAKE_MODEL=true`，导致断言“默认配置为False”的`test_settings_default_values`失败：1失败、340通过、82跳过，33.56秒；保留[首次离线日志](phase2b-0-offline-2026-11-11.log)。移除该进程运行参数后完整重跑得到341通过，未修改该测试或产品默认值。工程用例使用自身注入的Fake/Mock；没有真实模型调用或失败后切换模型。

本轮没有实现或验证2B-1启动/取消应用命令、监督器、完整受控续跑，也没有实现2B-2四专家图、真实Agent探针、2B-3 REST/SSE/运行页。没有新运行浏览器、Docker、真实模型或Embedding，没有改动STATUS、AGENTS、需求基线或上游设计。本次只验证2B-0为后续工作提供的持久化与提交隔离基础；完整审批、人工作业恢复、发布及External Write仍在后续阶段。

工作区包含用户和中断前留下的二A/设计/前端修改，均保留；本轮没有提交Git、推送或部署。源码及日志摘要索引见[执行证据JSON](phase2b-0-verification-2026-11-11.json)。
