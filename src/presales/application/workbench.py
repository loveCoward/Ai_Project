import asyncio
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from presales.access.policy import Policy
from presales.application.service import PresalesService
from presales.domain.contracts import AccessDenied, Actor, NotReady
from presales.indexing.embedding import ConfiguredEmbedding, FakeEmbedding
from presales.indexing.service import IndexService
from presales.infrastructure.config import OutboundSettings
from presales.infrastructure.database import rows
from presales.infrastructure.outbound import OutboundAdapter
from presales.knowledge.registry import KnowledgeRegistry
from presales.knowledge.review import KnowledgeReview
from presales.materials.models import PARSER_VERSION
from presales.materials.service import MaterialService
from presales.retrieval.service import ConfiguredReranker, RetrievalService


def selector_loop_factory():
    return asyncio.SelectorEventLoop()


class RetrievalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRESALES_RETRIEVAL_", env_file=None, extra="ignore"
    )
    dimensions: int | None = Field(default=None, ge=1, le=4096)


class Workbench:
    def __init__(self, service: PresalesService, retrieval: RetrievalService | None = None):
        self.service, self.database = service, service.database
        self.registry = KnowledgeRegistry(service)
        self.materials = MaterialService(service)
        self.review = KnowledgeReview(self.registry)
        self.retrieval = retrieval
        self.index = (
            IndexService(self.registry, self.materials, retrieval.embedding) if retrieval else None
        )

    def require_retrieval(self) -> RetrievalService:
        if self.retrieval is None:
            raise NotReady()
        return self.retrieval

    async def inputs(self, actor: Actor, task_id: UUID) -> dict:
        status = await self.materials.input_status(actor, task_id)
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await policy.resource(task_id)
            task = (
                await rows(
                    conn,
                    "SELECT revision,input_revision FROM presales.tasks WHERE id=:id",
                    id=task_id,
                )
            )[0]
            values = await rows(
                conn,
                """SELECT i.material_version_id,m.filename,j.id AS job_id,j.state,j.error_code,
                coalesce(man.necessity,'unclassified') AS necessity
                FROM presales.task_inputs i JOIN presales.material_versions mv ON mv.id=i.material_version_id
                JOIN presales.materials m ON m.id=mv.material_id
                LEFT JOIN presales.parse_jobs j ON j.source_version_id=i.material_version_id AND j.parser_version=:parser
                LEFT JOIN LATERAL (SELECT necessity FROM presales.task_input_manifest
                  WHERE task_id=i.task_id AND material_version_id=i.material_version_id
                  AND input_revision<=:revision ORDER BY input_revision DESC LIMIT 1) man ON true
                WHERE i.task_id=:task ORDER BY i.material_version_id""",
                parser=PARSER_VERSION,
                revision=task["input_revision"],
                task=task_id,
            )
            for item in values:
                source = (
                    await rows(
                        conn,
                        "SELECT resource_id FROM presales.resource_versions WHERE id=:id",
                        id=item["material_version_id"],
                    )
                )[0]
                await policy.resource(source["resource_id"])
            return {**status, "task_revision": task["revision"], "materials": values}

    async def overview(self, actor: Actor) -> dict:
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            resources = await rows(
                conn,
                "SELECT id,kind FROM presales.resources WHERE kind IN ('opportunity','task','knowledge') ORDER BY id",
            )
            visible = []
            for resource in resources:
                try:
                    await policy.resource(resource["id"])
                except AccessDenied:
                    continue
                visible.append({"id": str(resource["id"]), "kind": resource["kind"]})
            versions = []
            for resource in visible:
                if resource["kind"] != "knowledge":
                    continue
                candidates = await rows(
                    conn,
                    "SELECT id FROM presales.knowledge_versions WHERE document_id=:id ORDER BY id",
                    id=UUID(resource["id"]),
                )
                for version in candidates:
                    try:
                        value = await self.registry.version(
                            conn, policy, version["id"], ready=False
                        )
                    except AccessDenied:
                        continue
                    versions.append(
                        {
                            "version_id": str(value["id"]),
                            "document_id": resource["id"],
                            "filename": value["filename"],
                            "state": value["state"],
                            "revision": value["revision"],
                            "ready": value["ready"],
                        }
                    )
            return {
                "role": str(policy.role),
                "resources": visible,
                "knowledge_versions": versions,
                "authorization_revision": policy.authorization_revision,
                "catalog_revision": (
                    await rows(conn, "SELECT revision FROM presales.knowledge_catalog")
                )[0]["revision"],
                "retrieval_mode": self.retrieval.embedding.mode if self.retrieval else "disabled",
                "model_identity": self.retrieval.embedding.model_identity
                if self.retrieval
                else None,
            }

    async def detail(self, actor: Actor, version_id: UUID) -> dict:
        detail = await self.registry.inspect(actor, version_id)
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start()
            await self.registry.version(conn, policy, version_id, ready=False)
            builds = await rows(
                conn,
                "SELECT id,state,error_code,model_identity,mode,dimensions,chunk_count FROM presales.index_builds WHERE version_id=:id ORDER BY created_at,id",
                id=version_id,
            )
            jobs = await rows(
                conn,
                "SELECT id,state,error_code FROM presales.parse_jobs WHERE source_version_id=:id ORDER BY id",
                id=version_id,
            )
            reviews = await rows(
                conn,
                """SELECT r.id,r.action,r.version_revision,r.catalog_revision,d.approved
                FROM presales.knowledge_reviews r LEFT JOIN presales.knowledge_decisions d ON d.review_id=r.id
                WHERE r.version_id=:id ORDER BY r.created_at,r.id""",
                id=version_id,
            )
            return {**detail, "builds": builds, "parse_jobs": jobs, "reviews": reviews}


def configured_workbench(service: PresalesService) -> Workbench:
    settings = OutboundSettings()
    adapter = OutboundAdapter(settings)
    embedding = settings.embedding
    if embedding.mode == "disabled":
        return Workbench(service)
    dimensions = RetrievalSettings().dimensions
    if embedding.mode == "real" and dimensions is None:
        raise ValueError("Real retrieval requires explicit PRESALES_RETRIEVAL_DIMENSIONS")
    model = (
        FakeEmbedding(adapter, dimensions or 8)
        if embedding.mode == "fake"
        else ConfiguredEmbedding(
            adapter,
            dimensions or 8,
            embedding.api_key.get_secret_value() if embedding.api_key else None,
        )
    )
    reranker = (
        ConfiguredReranker(adapter, settings.reranker) if settings.reranker.mode == "real" else None
    )
    return Workbench(
        service, RetrievalService(KnowledgeRegistry(service), model, reranker=reranker)
    )
