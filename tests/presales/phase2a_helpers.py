from uuid import UUID, uuid4

from presales.domain.contracts import Actor, SourceDeclaration
from presales.indexing.embedding import FakeEmbedding
from presales.indexing.service import IndexService
from presales.infrastructure.config import OutboundServiceSettings, OutboundSettings
from presales.infrastructure.database import execute, rows
from presales.infrastructure.outbound import OutboundAdapter
from presales.knowledge.models import Applicability
from presales.knowledge.registry import KnowledgeRegistry
from presales.knowledge.review import KnowledgeReview
from presales.maintenance.service import AuthorizationChange, demo_id
from presales.materials.service import MaterialService

ARCH = Actor(subject=demo_id("architect"))
SALES = Actor(subject=demo_id("sales"))
REVIEWER = Actor(subject=demo_id("reviewer"))
OPP = demo_id("opportunity-a")
SCOPE = Applicability(products=("synthetic-product",), deployments=("private",))


def components(service, *, embedding=None, **kwargs):
    registry = KnowledgeRegistry(service)
    materials = MaterialService(service)
    fake = embedding or FakeEmbedding(
        OutboundAdapter(
            OutboundSettings(
                embedding=OutboundServiceSettings(
                    mode="fake", allowed_data_classes=frozenset({"synthetic"})
                )
            )
        )
    )
    return (
        registry,
        materials,
        IndexService(registry, materials, fake, **kwargs),
        KnowledgeReview(registry),
    )


async def grant(maintenance, resource_id, actors=(ARCH, REVIEWER, SALES)):
    preview = await maintenance.preview(
        [
            AuthorizationChange(kind="grant", principal_id=a.subject, resource_id=resource_id)
            for a in actors
        ]
    )
    await maintenance.apply(
        UUID(preview["change_id"]),
        preview["digest"],
        "Synthetic phase2a grants",
        confirm_expansion=True,
    )


async def candidate(
    services, *, text=b"# Synthetic product\n\nOwned test capability", scope=SCOPE, document_id=None
):
    service, maintenance, _, _ = services
    registry, materials, index, review = components(service)
    async with services[2].transaction() as conn:
        if not await rows(conn, "SELECT 1 FROM presales.knowledge_versions LIMIT 1"):
            await execute(conn, "UPDATE presales.knowledge_catalog SET revision=0")
    if document_id is None:
        document = await registry.create_document(ARCH, "Synthetic product", uuid4())
        document_id = UUID(document["document_id"])
        await grant(maintenance, document_id)
    version = await registry.register_version(
        ARCH, document_id, "synthetic.md", text, SourceDeclaration.SYNTHETIC, scope, uuid4()
    )
    version_id = UUID(version["version_id"])
    parsed = await materials.parse_material(ARCH, version_id)
    assert parsed["state"] == "succeeded"
    return registry, materials, index, review, document_id, version_id


async def publish(services):
    registry, materials, index, review, document, version = await candidate(services)
    build = await index.build(ARCH, version)
    submitted = await review.submit(ARCH, version, UUID(build["build_id"]), 1, 0, uuid4())
    decision = await review.decide(
        REVIEWER, UUID(submitted["review_id"]), True, "Synthetic approval", uuid4()
    )
    assert decision["state"] == "published"
    return registry, materials, index, review, document, version, UUID(build["build_id"])
