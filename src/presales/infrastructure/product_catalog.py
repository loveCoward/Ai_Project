"""Read-only stdio adapter with per-call, audience-bound backend credentials."""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

import jwt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from presales.domain.contracts import AccessDenied, Actor, AuthenticationFailed, DomainError
from presales.knowledge.models import Applicability
from presales.retrieval.models import EvidenceQuery, RetrievalResult


class CatalogSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_CATALOG_", env_file=None, extra="ignore", hide_input_in_errors=True
    )
    jwt_secret: SecretStr = Field(min_length=32, repr=False)


class CatalogResponse(BaseModel):
    status: Literal["ok", "empty", "not_found_or_not_allowed", "unavailable"]
    result: RetrievalResult | None = None
    error_code: str | None = None


class CatalogCredential:
    def __init__(self, secret: SecretStr):
        if len(secret.get_secret_value().encode()) < 32:
            raise ValueError("A dedicated signing key of at least 32 bytes is required")
        self._secret = secret

    def issue(self, actor: Actor, scope: Applicability) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "sub": str(actor.subject),
                "iss": "presales-backend",
                "aud": "presales-product-catalog",
                "iat": now,
                "nbf": now,
                "exp": now + 60,
                "scope": scope.model_dump(mode="json"),
            },
            self._secret.get_secret_value(),
            algorithm="HS256",
        )

    def verify(self, token: str, query: EvidenceQuery) -> Actor:
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError
            claims = jwt.decode(
                token,
                self._secret.get_secret_value(),
                algorithms=["HS256"],
                issuer="presales-backend",
                audience="presales-product-catalog",
                options={"require": ["sub", "iss", "aud", "exp", "iat", "nbf", "scope"]},
            )
            if (
                set(claims) != {"sub", "iss", "aud", "exp", "iat", "nbf", "scope"}
                or any(type(claims[k]) is not int for k in ("exp", "iat", "nbf"))
                or not 0 < claims["exp"] - claims["iat"] <= 60
                or claims["nbf"] != claims["iat"]
            ):
                raise ValueError
            if Applicability.model_validate(claims["scope"]) != query.scope:
                raise ValueError
            subject = UUID(claims["sub"])
            if str(subject) != claims["sub"]:
                raise ValueError
            return Actor(subject=subject)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthenticationFailed() from None


class CatalogAdapter:
    def __init__(self, retrieval, credential: CatalogCredential):
        self.retrieval, self.credential = retrieval, credential

    async def lookup(self, request: EvidenceQuery, token: str) -> CatalogResponse:
        try:
            actor = self.credential.verify(token, request)
            result = await self.retrieval.retrieve_evidence(actor, request)
            return CatalogResponse(status="ok" if result.hits else "empty", result=result)
        except (AccessDenied, AuthenticationFailed):
            return CatalogResponse(status="not_found_or_not_allowed")
        except DomainError as error:
            return CatalogResponse(status="unavailable", error_code=error.code)
        except Exception as error:
            return CatalogResponse(status="unavailable", error_code=type(error).__name__)


def create_catalog_server(adapter: CatalogAdapter, *, lifespan=None) -> FastMCP:
    server = FastMCP("Presales Product Catalog", lifespan=lifespan)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    async def lookup_capabilities(request: EvidenceQuery, ctx: Context) -> CatalogResponse:
        meta = ctx.request_context.meta
        envelope = meta.model_dump() if meta is not None else {}
        token = envelope.get("presales_context", "")
        return await adapter.lookup(request, token)

    return server


class CatalogClient:
    def __init__(
        self, credential: CatalogCredential, environment: dict[str, str], *, seconds: float = 10
    ):
        if not 0 < seconds <= 10:
            raise ValueError("Catalog deadline out of bounds")
        self.credential, self.seconds = credential, seconds
        allowed = {
            "PRESALES_DATABASE_URL",
            "PRESALES_FILE_ROOT",
            "PRESALES_CATALOG_JWT_SECRET",
            "PRESALES_RETRIEVAL_DIMENSIONS",
        }
        prefixes = ("PRESALES_OUTBOUND_EMBEDDING__", "PRESALES_OUTBOUND_RERANKER__")
        self.environment = {
            k: v for k, v in environment.items() if k in allowed or k.startswith(prefixes)
        }
        for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"):
            if name in os.environ:
                self.environment[name] = os.environ[name]
        self.environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

    async def lookup(self, actor: Actor, request: EvidenceQuery) -> CatalogResponse:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "presales.infrastructure.product_catalog"],
            env=self.environment,
        )
        try:
            async with asyncio.timeout(self.seconds):
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        token = self.credential.issue(actor, request.scope)
                        result = await session.call_tool(
                            "lookup_capabilities",
                            {"request": request.model_dump(mode="json")},
                            meta={"presales_context": token},
                        )
                        if result.isError or result.structuredContent is None:
                            return CatalogResponse(status="unavailable")
                        return CatalogResponse.model_validate(result.structuredContent)
        except Exception:
            return CatalogResponse(status="unavailable")


def main():
    from presales.application.service import PresalesService
    from presales.application.workbench import configured_workbench
    from presales.infrastructure.config import DatabaseSettings
    from presales.infrastructure.database import Database
    from presales.infrastructure.files import LocalFiles

    settings = DatabaseSettings()
    database = Database(settings)
    workbench = configured_workbench(PresalesService(database, LocalFiles(settings.file_root)))
    adapter = CatalogAdapter(
        workbench.require_retrieval(), CatalogCredential(CatalogSettings().jwt_secret)
    )

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield None
        finally:
            await database.close()

    server = create_catalog_server(adapter, lifespan=lifespan)
    # psycopg's Windows async connections require a selector loop at the entry point.
    if os.name == "nt":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.run_stdio_async())
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
