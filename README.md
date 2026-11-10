# B2B 售前证据化多智能体方案工作台

**BidDesk_zq** (`biddesk-zq`) 面向 B2B 软件与 IT 解决方案售前团队。

> **状态：业务需求已批准，完整售前工作流与核心前端尚未实现。** 当前仓库包含通用Agent服务/Streamlit脚手架、阶段一数据与授权基础、2A-0最小React工程、2A-1材料解析/知识治理/索引构建的应用服务与持久化，以及登录/JWT和少量业务读取入口；2A-1未开放对应业务HTTP或页面，不代表已完成阶段验收或产品验收。最新范围见[项目状态](STATUS.md)。

已批准的需求基线和决策记录：

- [初版业务需求分析文档](docs/superpowers/specs/初版业务需求分析文档.md)
- [售前需求决策记录](docs/presales-requirements-decisions.md)
- [领域术语表](GLOSSARY.md)

需求批准不等于实现授权、部署授权或验收通过。当前脚手架仅供工程开发和联通验证；请勿用于生产业务，或输入真实客户敏感材料。演示及测试数据应遵循已批准需求文档中的数据边界。

## 工作台方向与交付顺序

已确认保留Streamlit用于聊天演示与内部验证，新增React + TypeScript + Vite作为任务、证据、方案编辑、审阅审批与发布恢复的核心工作台。业务操作通过FastAPI REST API，进度通过SSE；持久状态由后端管理，断线后重新鉴权并读取当前快照。

建议二A交付最小前端基础与材料/知识页面，二B前完成；二B接入后台分析和SSE，三/四分别同步交付审批和发布恢复页面，五完成体验、导出与双入口Compose验证。`frontend/`已有2A-0最小React工程（登录、身份校验、受保护路由、草稿/材料受限读取），材料/知识页面、SSE和其余业务页面尚未实现；范围与验证要求见[双入口接入设计](docs/superpowers/specs/双入口工作台与前端分阶段接入设计.md)。

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
