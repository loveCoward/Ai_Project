"""Read-only Opportunity Workspace stdio server; no write tools are registered."""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from presales.application.opportunity_context import ContextOutcome
from presales.domain.contracts import AccessDenied, Actor, AuthenticationFailed

READ_CONTEXT_SECONDS = 10


class WorkspaceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_WORKSPACE_", env_file=None, extra="ignore", hide_input_in_errors=True
    )
    jwt_secret: SecretStr = Field(min_length=32, repr=False)


class ReadContextRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: UUID
    attempt_id: UUID | None = None


class WorkspaceCredential:
    """Issues short-lived, opportunity-scoped tokens; never accepts caller identity."""

    def __init__(self, secret: SecretStr) -> None:
        if len(secret.get_secret_value().encode()) < 32:
            raise ValueError("A dedicated signing key of at least 32 bytes is required")
        self._secret = secret

    def issue(self, actor: Actor, opportunity_id: UUID, attempt_id: UUID | None = None) -> str:
        now = int(time.time())
        claims = {
            "sub": str(actor.subject),
            "iss": "presales-backend",
            "aud": "presales-opportunity-workspace",
            "iat": now,
            "nbf": now,
            "exp": now + 60,
            "opportunity": str(opportunity_id),
        }
        if attempt_id is not None:
            claims["attempt"] = str(attempt_id)
        return jwt.encode(
            claims,
            self._secret.get_secret_value(),
            algorithm="HS256",
        )

    def verify(self, token: str, request: ReadContextRequest) -> Actor:
        try:
            if not isinstance(token, str) or not token or len(token) > 8192:
                raise ValueError
            claims = jwt.decode(
                token,
                self._secret.get_secret_value(),
                algorithms=["HS256"],
                issuer="presales-backend",
                audience="presales-opportunity-workspace",
                options={"require": ["sub", "iss", "aud", "exp", "iat", "nbf", "opportunity"]},
            )
            required = {"sub", "iss", "aud", "exp", "iat", "nbf", "opportunity"}
            if (
                set(claims) not in (required, required | {"attempt"})
                or any(type(claims[key]) is not int for key in ("exp", "iat", "nbf"))
                or not 0 < claims["exp"] - claims["iat"] <= 60
                or claims["nbf"] != claims["iat"]
            ):
                raise ValueError
            if claims["opportunity"] != str(request.opportunity_id):
                raise ValueError
            if not _attempt_matches(claims.get("attempt"), request.attempt_id):
                raise ValueError
            subject = UUID(claims["sub"])
            if str(subject) != claims["sub"]:
                raise ValueError
            return Actor(subject=subject)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthenticationFailed() from None


def _attempt_matches(claimed: str | None, requested: UUID | None) -> bool:
    if claimed is None:
        return requested is None
    if requested is None or claimed != str(requested):
        return False
    try:
        return str(UUID(claimed)) == claimed
    except ValueError:
        return False


def _is_timeout(error: BaseException) -> bool:
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, TimeoutError):
            return True
        pending.extend(current.exceptions if isinstance(current, BaseExceptionGroup) else ())
    return False


class WorkspaceAdapter:
    def __init__(
        self,
        context,
        credential: WorkspaceCredential,
        *,
        read_seconds: float = READ_CONTEXT_SECONDS,
    ) -> None:
        if not 0 < read_seconds <= READ_CONTEXT_SECONDS:
            raise ValueError("Workspace read deadline out of bounds")
        self.context, self.credential = context, credential
        self.read_seconds = read_seconds

    async def read_context(self, request: ReadContextRequest, token: str) -> dict[str, Any]:
        try:
            actor = self.credential.verify(token, request)
        except (AccessDenied, AuthenticationFailed):
            return {"status": "not_found_or_not_allowed"}
        try:
            outcome = await self.context.read(
                actor, request.opportunity_id, seconds=self.read_seconds
            )
        except Exception:
            return {"status": "unavailable"}
        return outcome.model_dump(mode="json")


def create_workspace_server(adapter: WorkspaceAdapter, *, lifespan=None) -> FastMCP:
    server = FastMCP("Presales Opportunity Workspace", lifespan=lifespan)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    async def read_context(request: ReadContextRequest, ctx: Context) -> dict[str, Any]:
        meta = ctx.request_context.meta
        envelope = meta.model_dump() if meta is not None else {}
        token = envelope.get("presales_context", "")
        return await adapter.read_context(request, token)

    return server


class WorkspaceClient:
    """Spawns a short-lived stdio child per read; only the backend may sign credentials."""

    def __init__(
        self,
        credential: WorkspaceCredential,
        environment: dict[str, str],
        *,
        seconds: float = READ_CONTEXT_SECONDS,
    ) -> None:
        if not 0 < seconds <= READ_CONTEXT_SECONDS:
            raise ValueError("Workspace deadline out of bounds")
        self.credential, self.seconds = credential, seconds
        allowed = {
            "PRESALES_DATABASE_URL",
            "PRESALES_FILE_ROOT",
            "PRESALES_WORKSPACE_JWT_SECRET",
        }
        self.environment = {key: value for key, value in environment.items() if key in allowed}
        for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"):
            if name in os.environ:
                self.environment[name] = os.environ[name]
        self.environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

    async def read_context(self, actor: Actor, request: ReadContextRequest) -> ContextOutcome:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "presales.infrastructure.opportunity_workspace"],
            env=self.environment,
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.seconds):
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        token = self.credential.issue(
                            actor, request.opportunity_id, request.attempt_id
                        )
                        result = await session.call_tool(
                            "read_context",
                            {"request": request.model_dump(mode="json")},
                            meta={"presales_context": token},
                        )
                        if result.isError or result.structuredContent is None:
                            return ContextOutcome(status="unavailable")
                        return ContextOutcome.model_validate(result.structuredContent)
        except BaseException as error:
            if time.monotonic() - started >= self.seconds or _is_timeout(error):
                return ContextOutcome(status="timeout", error_code="context_deadline_exceeded")
            if isinstance(error, Exception):
                return ContextOutcome(status="unavailable")
            raise


def main() -> None:
    from presales.application.opportunity_context import OpportunityContextReader
    from presales.infrastructure.config import DatabaseSettings
    from presales.infrastructure.database import Database

    settings = DatabaseSettings()
    database = Database(settings)
    adapter = WorkspaceAdapter(
        OpportunityContextReader(database),
        WorkspaceCredential(WorkspaceSettings().jwt_secret),
    )

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield None
        finally:
            await database.close()

    server = create_workspace_server(adapter, lifespan=lifespan)
    # psycopg's Windows async connections require a selector loop at the entry point.
    if os.name == "nt":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.run_stdio_async())
    else:
        server.run(transport="stdio")


__all__ = [
    "ReadContextRequest",
    "WorkspaceAdapter",
    "WorkspaceClient",
    "WorkspaceCredential",
    "WorkspaceSettings",
    "create_workspace_server",
    "main",
]


if __name__ == "__main__":
    main()
