from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from presales.access.jwt import JwtVerifier
from presales.api.routes import register_business_routes
from presales.application.auth import AuthenticationService, PrincipalStore
from presales.application.service import PresalesService
from presales.domain.contracts import Actor, AuthenticationFailed


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class LoginResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: str
    expires_in: int


def actor_dependency(verifier: JwtVerifier, principal_store: PrincipalStore):
    async def current_actor(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(HTTPBearer(auto_error=False)),
        ],
    ) -> Actor:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            actor = verifier.verify(credentials.credentials)
        except AuthenticationFailed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        principal = await principal_store.find(actor.subject)
        if principal is None or not principal.active or principal.subject != actor.subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return actor

    return current_actor


def create_auth_app(
    authentication_service: AuthenticationService,
    verifier: JwtVerifier,
    *,
    shutdown: Callable[[], Awaitable[None]] | None = None,
    compatibility_app: FastAPI | None = None,
    business_service: PresalesService | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if compatibility_app is None:
                yield
            else:
                async with compatibility_app.router.lifespan_context(compatibility_app):
                    yield
        finally:
            if shutdown is not None:
                await shutdown()

    app = FastAPI(title="Presales Authentication", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> Response:
        return Response(
            content='{"detail":"Invalid request"}',
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/auth/login", response_model=LoginResponse)
    async def login(body: LoginRequest, response: Response) -> LoginResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await authentication_service.login(body.username, body.password)
        except AuthenticationFailed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            ) from None
        return LoginResponse(
            access_token=result.access_token,
            token_type=result.token_type,
            expires_in=result.expires_in,
        )

    current_actor = actor_dependency(verifier, authentication_service.principal_store)

    @app.get("/auth/me")
    async def identity(
        actor: Annotated[Actor, Depends(current_actor)], response: Response
    ) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"subject": str(actor.subject)}

    if business_service is not None:
        register_business_routes(app, business_service, current_actor)

    if compatibility_app is not None:
        app.mount("/compat", compatibility_app, name="generic-compatibility")

    return app
