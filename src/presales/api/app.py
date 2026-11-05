from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from presales.access.jwt import JwtVerifier
from presales.api.auth import create_auth_app
from presales.application.auth import AuthenticationService, demo_accounts
from presales.application.service import PresalesService
from presales.infrastructure.config import (
    DatabaseSettings,
    DemoAccountsSettings,
    JwtSettings,
    OutboundSettings,
    PresalesApiSettings,
)
from presales.infrastructure.database import Database
from presales.infrastructure.files import LocalFiles
from presales.infrastructure.outbound import OutboundAdapter
from presales.infrastructure.principals import DatabasePrincipalStore


def _load_compatibility_app(path: Path) -> FastAPI:
    from core.settings import DatabaseType, settings

    if settings.AUTH_SECRET is None:
        raise ValueError("Compatibility mode requires the generic service AUTH_SECRET")
    if settings.DATABASE_TYPE != DatabaseType.SQLITE:
        raise ValueError("Compatibility mode requires an isolated SQLite generic service")
    if settings.LANGFUSE_TRACING or settings.LANGCHAIN_TRACING_V2:
        raise ValueError("Compatibility mode requires generic tracing to be disabled")
    if not path.parent.is_dir() or path.is_dir():
        raise ValueError("Compatibility checkpoint path must be a file in an existing directory")
    if Path(settings.SQLITE_DB_PATH).resolve() == path.resolve():
        raise ValueError("Compatibility checkpoint must be separate from the generic checkpoint")

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.store.memory import InMemoryStore

    from agents import get_agent, get_all_agent_info, load_agent
    from service.service import app as generic_app

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncSqliteSaver.from_conn_string(str(path.resolve())) as saver:
            await saver.setup()
            store = InMemoryStore()
            for agent_info in get_all_agent_info():
                await load_agent(agent_info.key)
                agent = get_agent(agent_info.key)
                agent.checkpointer = saver
                agent.store = store
            yield

    compatibility_app = FastAPI(
        title="Generic Scaffold Compatibility",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    compatibility_app.mount("/", generic_app)
    return compatibility_app


def create_app() -> FastAPI:
    api_settings = PresalesApiSettings()
    database_settings = DatabaseSettings()
    jwt_settings = JwtSettings()
    account_settings = DemoAccountsSettings()
    outbound_settings = OutboundSettings()
    compatibility_app = None
    if api_settings.compatibility_mode:
        if api_settings.compatibility_checkpoint_path is None:
            raise ValueError("Compatibility mode requires a dedicated checkpoint path")
        compatibility_app = _load_compatibility_app(api_settings.compatibility_checkpoint_path)

    database = Database(database_settings)
    authentication_service = AuthenticationService(
        DatabasePrincipalStore(database), jwt_settings, demo_accounts(account_settings)
    )
    app = create_auth_app(
        authentication_service,
        JwtVerifier(jwt_settings),
        shutdown=database.close,
        compatibility_app=compatibility_app,
        business_service=PresalesService(database, LocalFiles(database_settings.file_root)),
    )
    app.state.outbound = OutboundAdapter(outbound_settings)
    return app
