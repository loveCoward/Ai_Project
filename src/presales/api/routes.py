from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response

from presales.application.service import PresalesService
from presales.domain.contracts import AccessDenied, Actor, NotReady, StorageUnavailable


def register_business_routes(
    app: FastAPI, service: PresalesService, current_actor: Callable
) -> None:
    router = APIRouter(prefix="/api")

    async def no_client_context(request: Request) -> None:
        if request.query_params or await request.body():
            raise HTTPException(status_code=422, detail="Invalid request")

    @router.get("/materials/{version_id}")
    async def read_material(
        version_id: UUID, actor: Annotated[Actor, Depends(current_actor)], request: Request
    ) -> Response:
        await no_client_context(request)
        try:
            data = await service.read_material(actor, version_id)
        except AccessDenied:
            raise HTTPException(status_code=404, detail="Not found or not allowed") from None
        except NotReady:
            raise HTTPException(status_code=409, detail="Input not ready") from None
        except StorageUnavailable:
            raise HTTPException(status_code=503, detail="Storage unavailable") from None
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/drafts/{revision_id}")
    async def read_draft(
        revision_id: UUID,
        actor: Annotated[Actor, Depends(current_actor)],
        request: Request,
        response: Response,
    ) -> dict:
        await no_client_context(request)
        try:
            body = await service.read_draft(actor, revision_id)
        except AccessDenied:
            raise HTTPException(status_code=404, detail="Not found or not allowed") from None
        except StorageUnavailable:
            raise HTTPException(status_code=503, detail="Storage unavailable") from None
        response.headers["Cache-Control"] = "no-store"
        return body

    app.include_router(router)
