# B2B 售前证据化多智能体方案工作台

**BidDesk_zq** (`biddesk-zq`) 面向 B2B 软件与 IT 解决方案售前团队。

> **状态：业务需求已批准，功能尚未实现。** 当前仓库仍包含从上游项目继承的通用 Agent 服务和 Streamlit 对话脚手架；它们不是 BidDesk_zq 的售前业务功能，也不代表已完成产品验收。

已批准的需求基线和决策记录：

- [初版业务需求分析文档](docs/superpowers/specs/初版业务需求分析文档.md)
- [售前需求决策记录](docs/presales-requirements-decisions.md)
- [领域术语表](GLOSSARY.md)

需求批准不等于实现授权、部署授权或验收通过。当前脚手架仅供工程开发和联通验证；请勿用于生产业务，或输入真实客户敏感材料。演示及测试数据应遵循已批准需求文档中的数据边界。



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
uv run pymarkdown scan README.md docs/
```

本仓库现有代码、测试和 CI 针对继承的通用 Agent 脚手架；不能据此推断 BidDesk_zq 业务功能已实现或通过验收。
