import base64
import binascii
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from presales.application.workbench import Workbench
from presales.domain.contracts import Actor, DomainError, SourceDeclaration
from presales.indexing.embedding import EmbeddingFailure
from presales.infrastructure.outbound import OutboundConfigurationError
from presales.knowledge.models import Applicability, KnowledgeAction
from presales.materials.models import ManifestItem
from presales.retrieval.models import Evidence, EvidenceQuery, RetrievalResult


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Upload(StrictBody):
    filename: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(max_length=27_962_028)
    declaration: SourceDeclaration
    request_id: UUID

    def data(self) -> bytes:
        try:
            data = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "Invalid request") from None
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(422, "Invalid request")
        return data


class KnowledgeUpload(Upload):
    scope: Applicability


class MaterialUpload(Upload):
    expected_task_revision: int | None = Field(default=None, ge=1)


class IndexRequest(StrictBody):
    replaces: UUID | None = None


class SubmitRequest(StrictBody):
    build_id: UUID
    expected_revision: int = Field(ge=1)
    expected_catalog: int = Field(ge=0)
    request_id: UUID
    action: KnowledgeAction = "publish"
    replaces: UUID | None = None


class DecisionRequest(StrictBody):
    approved: bool
    reason: str = Field(min_length=1, max_length=1000)
    request_id: UUID


class ManifestRequest(StrictBody):
    expected_revision: int = Field(ge=1)
    items: list[ManifestItem] = Field(max_length=10)


class ScopeRequest(StrictBody):
    scope: Applicability
    expected_revision: int = Field(ge=1)
    expected_catalog: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)
    request_id: UUID


def register_knowledge_routes(app: FastAPI, workbench: Workbench, current_actor):
    async def no_query(request: Request):
        if request.query_params or (request.method == "GET" and await request.body()):
            raise HTTPException(422, "Invalid request")

    router = APIRouter(prefix="/api", dependencies=[Depends(no_query)])
    actor_type = Annotated[Actor, Depends(current_actor)]

    @app.middleware("http")
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_, error):
        from fastapi.responses import JSONResponse

        code = error.code
        status = (
            404
            if code == "not_found_or_not_allowed"
            else 503
            if code.startswith("storage_")
            else 422
            if code == "invalid_input"
            else 409
        )
        return JSONResponse(
            {"detail": code}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    async def configuration_error(_, __):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": "retrieval_service_not_configured_or_failed"}, status_code=503
        )

    app.add_exception_handler(OutboundConfigurationError, configuration_error)
    app.add_exception_handler(EmbeddingFailure, configuration_error)

    @router.get("/workbench")
    async def overview(actor: actor_type):
        return await workbench.overview(actor)

    @router.get("/knowledge/{version_id}")
    async def detail(version_id: UUID, actor: actor_type):
        return await workbench.detail(actor, version_id)

    @router.post("/knowledge/documents/{document_id}/versions")
    async def upload_knowledge(document_id: UUID, body: KnowledgeUpload, actor: actor_type):
        return await workbench.registry.register_version(
            actor,
            document_id,
            body.filename,
            body.data(),
            body.declaration,
            body.scope,
            body.request_id,
        )

    @router.post("/materials/{parent_id}/versions")
    async def upload_material(parent_id: UUID, body: MaterialUpload, actor: actor_type):
        return await workbench.service.register_material(
            actor,
            parent_id,
            body.filename,
            body.data(),
            body.declaration,
            body.request_id,
            body.expected_task_revision,
        )

    @router.post("/sources/{version_id}/parse")
    async def parse(version_id: UUID, request: Request, actor: actor_type):
        if await request.body():
            raise HTTPException(422, "Invalid request")
        return await workbench.materials.parse_material(actor, version_id)

    @router.get("/parse-jobs/{job_id}")
    async def parsed(job_id: UUID, actor: actor_type):
        return await workbench.materials.get_parse_result(actor, job_id)

    @router.get("/tasks/{task_id}/inputs")
    async def inputs(task_id: UUID, actor: actor_type):
        return await workbench.inputs(actor, task_id)

    @router.post("/tasks/{task_id}/manifest")
    async def manifest(task_id: UUID, body: ManifestRequest, actor: actor_type):
        return await workbench.materials.set_manifest(
            actor, task_id, body.expected_revision, body.items
        )

    @router.post("/knowledge/{version_id}/index")
    async def index(version_id: UUID, body: IndexRequest, actor: actor_type):
        workbench.require_retrieval()
        assert workbench.index is not None
        return await workbench.index.build(actor, version_id, replaces=body.replaces)

    @router.post("/knowledge/{version_id}/submit")
    async def submit(version_id: UUID, body: SubmitRequest, actor: actor_type):
        return await workbench.review.submit(
            actor,
            version_id,
            body.build_id,
            body.expected_revision,
            body.expected_catalog,
            body.request_id,
            action=body.action,
            replaces=body.replaces,
        )

    @router.post("/knowledge/{version_id}/scope")
    async def scope(version_id: UUID, body: ScopeRequest, actor: actor_type):
        return await workbench.review.adjust_candidate_scope(
            actor,
            version_id,
            body.scope,
            body.expected_revision,
            body.expected_catalog,
            body.reason,
            body.request_id,
        )

    @router.post("/knowledge-reviews/{review_id}/decision")
    async def decide(review_id: UUID, body: DecisionRequest, actor: actor_type):
        return await workbench.review.decide(
            actor, review_id, body.approved, body.reason, body.request_id
        )

    @router.post("/evidence/retrieve", response_model=RetrievalResult)
    async def retrieve(body: EvidenceQuery, actor: actor_type):
        return await workbench.require_retrieval().retrieve_evidence(actor, body)

    @router.get("/evidence/{evidence_id}", response_model=Evidence)
    async def evidence(evidence_id: UUID, actor: actor_type):
        return await workbench.require_retrieval().read_evidence(actor, evidence_id)

    @router.post("/catalog/lookup")
    async def catalog(body: EvidenceQuery, actor: actor_type):
        import os

        from presales.infrastructure.product_catalog import (
            CatalogClient,
            CatalogCredential,
            CatalogSettings,
        )

        # The current backend identity is transported outside tool business arguments.
        try:
            credential = CatalogCredential(CatalogSettings().jwt_secret)
        except ValidationError:
            raise HTTPException(503, "catalog_not_configured") from None
        return await CatalogClient(credential, dict(os.environ)).lookup(actor, body)

    app.include_router(router)
