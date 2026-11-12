from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from presales.domain.contracts import StorageUnavailable
from presales.infrastructure.config import DatabaseSettings

AUTH_LOCK = 1
GRAPH_LOCK = 2
LOCK_NAMESPACE = 72401

"""售前模块的PostgreSQL数据库访问层。提供了数据库连接、事务管理、行查询和执行SQL语句的功能。
"""


async def rows(conn: AsyncConnection, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = await conn.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def execute(conn: AsyncConnection, sql: str, **params: Any) -> None:
    await conn.execute(text(sql), params)


async def lock(conn: AsyncConnection, key: int, *, shared: bool = False) -> None:
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await execute(conn, f"SELECT {function}(:namespace, :key)", namespace=LOCK_NAMESPACE, key=key)


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.pool_size,
            max_overflow=0,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 5},
            isolation_level="READ COMMITTED",
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncConnection, None]:
        try:
            async with self.engine.begin() as conn:
                await execute(
                    conn,
                    "SELECT set_config('lock_timeout', :value, true)",
                    value=f"{self.settings.lock_timeout_ms}ms",
                )
                await execute(
                    conn,
                    "SELECT set_config('statement_timeout', :value, true)",
                    value=f"{self.settings.statement_timeout_ms}ms",
                )
                yield conn
        except SQLAlchemyError:
            raise StorageUnavailable() from None

    async def health(self) -> bool:
        async with self.transaction() as conn:
            await execute(conn, "SELECT 1")
        return True

    async def close(self) -> None:
        await self.engine.dispose()
