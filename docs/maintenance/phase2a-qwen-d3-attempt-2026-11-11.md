# D3千问Embedding预检执行记录

日期：2026-11-11（Asia/Shanghai）。本次仅续作D3预检和执行入口，不改需求、设计结论、STATUS或此前交付记录，不安装依赖、提交Git或部署。

## 本次交付说明

用户确认真实Embedding模型`qwen3.7-text-embedding-flash`、1024维及完整端点`https://maas.qianwenaiapi.com/compatible-mode/v1/embeddings`；允许合成文本外发，不设金额上限，保留20请求、12,000 Unicode字符、600秒及单请求60秒预算，索引批次最多20条。

新增单条预检门禁：先核实专用凭据，使用已有ConfiguredEmbedding检查响应model精确匹配、data索引，再检查向量数量、1024维、每个元素为有限数值且非布尔。预检成功后才能进入真实临时数据库D3。D3内部共用同一BudgetedEmbedding累计预检/索引/查询及重试；显式索引batch_size=20，每次调用前再拒绝超过20条的批次。正常路径预计16请求（预检1、索引5、查询10），不静默切Fake。

只从本地专用Embedding环境变量或项目`.env`读取凭据；不将OPENAI_API_KEY、COMPATIBLE_API_KEY或无明确用途的API_KEY转发给新端点。凭据和原始向量不进入输出。此前聊天模型配置不构成本次Embedding凭据。

实际结果：**preflight_blocked / embedding_credential_not_injected**。在当前进程、本机用户/机器环境以及项目`.env`中未发现所列专用凭据。`privatecredentials`仅有`.gitkeep`，未读取其他秘密或尝试其他接收端。0请求、0字符外发；未获得实际响应，不能声称响应身份/维度/格式已通过；D3未启动，未启动本次数据库实例或索引，没有业务数据变更。

## 本轮文件范围

| 文件 | 本次作用 |
| --- | --- |
| `scripts/probe_presales_retrieval.py` | 固定用户确认模型/端点/维度；专用凭据解析；预检门禁；累计预算、每批20条限制和调用元数据；独立预检CLI |
| `tests/presales/test_phase2a_tools.py` | 真实适配器MockTransport正反例：模型不符、索引不符、维度不符、布尔/非数值、重定向、缺凭据拒用聊天Key；请求/字符/时间/批次限制 |
| `docs/superpowers/plans/2026-11-11-phase-two-a-prerequisites.md` | 追加用户确认配置、执行顺序和累计预算说明 |
| `docs/maintenance/phase2a-qwen-embedding-preflight-2026-11-11.json` | 本次真实模式预检阻塞记录，0调用；保留旧报告 |
| 本报告 | 独立记录本次实际执行、验证与D3剩余门禁 |

其余既有工作区改动不属于本轮增量，未修改或回滚。开工已有8个已跟踪修改文件（合计189新增/18删除）以及此前实现/报告等未跟踪文件，本轮仅在上表三个已有文件追加本次必要改动，并新增两个报告。

## 验证命令与实际结果

| 命令原文 | 实际结果 |
| --- | --- |
| `git status --short`；`git diff --stat` | 已执行；记录开工既有差异，不归入本轮交付 |
| `uv run --no-sync pytest tests/presales/test_phase2a_tools.py tests/presales/test_embedding_adapter.py -q -rs` | 16通过、0失败、0跳过；MockTransport不产生真实调用 |
| `$env:PYTHONPATH = 'src'; uv run --no-sync python scripts/probe_presales_retrieval.py --verify-embedding --output docs/maintenance/phase2a-qwen-embedding-preflight-2026-11-11.json` | 非零退出，报告preflight_blocked；0请求、0字符；未获得供应方响应 |
| `.venv/Scripts/ruff.exe check scripts/probe_presales_retrieval.py tests/presales/test_phase2a_tools.py` | 通过，0违规 |
| `.venv/Scripts/ruff.exe format --check scripts/probe_presales_retrieval.py tests/presales/test_phase2a_tools.py` | 通过，2文件已格式化 |
| `.venv/Scripts/pymarkdown.exe scan docs/superpowers/plans/2026-11-11-phase-two-a-prerequisites.md docs/maintenance/phase2a-qwen-d3-attempt-2026-11-11.md` | 通过，0问题 |
| D3真实检索、PostgreSQL专项、全量pytest、前端检查 | 未执行；预检凭据门禁未满足，且本轮仅改探针入口/对应测试/报告，未改共享应用或前端；不将历史结果当本次通过 |

## 剩余退出证据

D3/架构第7.1节仍缺：一次成功的真实响应预检，以及预检通过后固定开发样例的实际运行和独立报告。此次失败属于本地配置缺失，不是供应方模型已经被证明基本不可行，也不能据此声称语义质量有差距或达标。

需要在本地为`PRESALES_OUTBOUND_EMBEDDING__API_KEY`安全注入对应端点的Embedding凭据，再执行含即时预检门禁的D3；无需把Key发进聊天。金额上限不设，但请求/字符/时间上限继续生效。默认自动化、固定数据和旧失败记录均保留。
