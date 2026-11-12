# B2B 售前证据化多智能体方案工作台

**BidDesk_zq** (`biddesk-zq`) 面向 B2B 软件与 IT 解决方案售前团队。

> **状态：业务需求已批准，完整售前工作流与分析工作台尚未实现。** 当前仓库包含通用Agent服务/Streamlit脚手架、阶段一数据与授权基础、2A-0最小React工程、2A-1材料/知识应用服务、2A-2共享受限检索与只读Product Catalog MCP、2A-3最小材料/知识工作台与业务HTTP入口、2B-0的Attempt契约/方案Schema/持久化/受控checkpoint提交基础，以及2B-1任务7至12的Attempt授权边界、主动预算持久累计、显式模型传输适配、单事务启动命令、应用内监督器、架构师取消命令与执行停止后条件排空、重启核实与受控继续（含真实子进程并发验证），以及2B-2任务13的商机只读背景读取与只读stdio服务入口（背景必需性、七类读取结局、来源契约）。2B尚未接入分析运行时REST命令、分析图或SSE，stdio真实进程往返验证未完成；其工程验证不代表二B整体完成、阶段验收、部署批准或真实Agent能力达标。最新范围见[项目状态](STATUS.md)。

已批准的需求基线和决策记录：

- [初版业务需求分析文档](docs/superpowers/specs/初版业务需求分析文档.md)
- [售前需求决策记录](docs/presales-requirements-decisions.md)
- [领域术语表](GLOSSARY.md)

需求批准不等于实现授权、部署授权或验收通过。当前脚手架仅供工程开发和联通验证；请勿用于生产业务，或输入真实客户敏感材料。演示及测试数据应遵循已批准需求文档中的数据边界。

## 工作台方向与交付顺序

已确认保留Streamlit用于聊天演示与内部验证，新增React + TypeScript + Vite作为任务、证据、方案编辑、审阅审批与发布恢复的核心工作台。业务操作通过FastAPI REST API，进度通过SSE；持久状态由后端管理，断线后重新鉴权并读取当前快照。

规划顺序为二A交付最小前端基础与材料/知识页面，二B接入后台分析和SSE，三/四分别交付方案审批和发布恢复页面，五完成体验、导出与双入口Compose验证。`frontend/`已有2A-0认证基础及2A-3最小材料/知识页面；分析运行页、SSE、方案审批与发布恢复页面尚未实现。范围与验证要求见[双入口接入设计](docs/superpowers/specs/双入口工作台与前端分阶段接入设计.md)。

## 本地运行当前脚手架

以下步骤只启动仓库当前继承的通用 Agent 服务及聊天页面，不会提供售前需求分析、证据管理、方案生成、审批或发布等 BidDesk_zq 业务能力。

环境要求：Python 3.12–3.14、[uv](https://docs.astral.sh/uv/)。运行模型需要配置相应服务商的 API 凭据；请仅使用获准的测试数据。

1. 复制环境变量模板并按需配置：

   ```sh
   cp .env.example .env
   ```

2. 安装锁定的依赖：

   ```sh
   uv sync --frozen
   ```

3. 在一个终端启动通用 Agent 服务：

   ```sh
   uv run python src/run_service.py
   ```

4. 在另一个终端启动 Streamlit 脚手架页面：

   ```sh
   uv run streamlit run src/streamlit_app.py
   ```

如需使用仓库现有 Docker Compose 开发配置，可在完成 `.env` 配置后运行：

```sh
docker compose watch
```

## 开发检查

```sh
uv run pytest
uv run ruff check
uv run pymarkdown scan AGENTS.md STATUS.md README.md docs/
```

前端工程检查在`frontend/`下运行，不需要模型 Key：

```sh
npm ci
npm run typecheck
npm run build
```

本仓库包含继承脚手架与部分售前基础测试；不能据此推断完整售前流程或核心工作台已实现、产品验收已通过。前端目前只有类型检查和构建检查，没有单元测试或浏览器E2E；真实PostgreSQL专项验证需按[维护说明](docs/maintenance/presales-phase-one.md)显式运行。
