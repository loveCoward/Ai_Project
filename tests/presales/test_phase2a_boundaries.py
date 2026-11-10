import asyncio
from uuid import UUID, uuid4

import pytest

from presales.access.policy import Policy
from presales.domain.contracts import (
    AccessDenied,
    InvalidInput,
    SourceDeclaration,
    StorageUnavailable,
)
from presales.indexing.chunks import ChunkConfig, split_chunks
from presales.indexing.embedding import FakeEmbedding
from presales.infrastructure.database import execute, rows
from presales.maintenance.service import AuthorizationChange
from presales.materials.service import MaterialService
from tests.presales.phase2a_helpers import ARCH, OPP, REVIEWER, candidate, components

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_task_file_count_and_total_byte_boundaries(services):
    service = services[0]
    task = await service.create_task(ARCH, OPP, "Synthetic file boundary", uuid4())
    task_id = UUID(task["resource_id"])
    for index in range(10):
        await service.register_material(
            ARCH,
            task_id,
            "x.md",
            b"x",
            SourceDeclaration.SYNTHETIC,
            uuid4(),
            expected_task_revision=index + 1,
        )
    with pytest.raises(InvalidInput):
        await service.register_material(
            ARCH,
            task_id,
            "x.md",
            b"x",
            SourceDeclaration.SYNTHETIC,
            uuid4(),
            expected_task_revision=11,
        )
    task = await service.create_task(ARCH, OPP, "Synthetic byte boundary", uuid4())
    task_id = UUID(task["resource_id"])
    for revision, size in enumerate((20, 20, 10), 1):
        await service.register_material(
            ARCH,
            task_id,
            "x.md",
            b"x" * (size * 1024 * 1024),
            SourceDeclaration.SYNTHETIC,
            uuid4(),
            expected_task_revision=revision,
        )
    with pytest.raises(InvalidInput):
        await service.register_material(
            ARCH,
            task_id,
            "x.md",
            b"x",
            SourceDeclaration.SYNTHETIC,
            uuid4(),
            expected_task_revision=4,
        )
    with pytest.raises(InvalidInput):
        await service.register_material(
            ARCH, OPP, "x.md", b"x" * (20 * 1024 * 1024 + 1), SourceDeclaration.SYNTHETIC, uuid4()
        )


async def test_task_character_boundary_and_failed_retry_preserves_job(services):
    service = services[0]
    task = await service.create_task(ARCH, OPP, "Synthetic character boundary", uuid4())
    task_id = UUID(task["resource_id"])
    materials = MaterialService(service)
    for index in range(3):
        source = await service.register_material(
            ARCH,
            task_id,
            "x.md",
            ("中" * 100_000).encode(),
            SourceDeclaration.SYNTHETIC,
            uuid4(),
            expected_task_revision=index + 1,
        )
        assert (await materials.parse_material(ARCH, UUID(source["material_version_id"])))[
            "state"
        ] == "succeeded"
    source = await service.register_material(
        ARCH, task_id, "x.md", b"x", SourceDeclaration.SYNTHETIC, uuid4(), expected_task_revision=4
    )
    first = await materials.parse_material(ARCH, UUID(source["material_version_id"]))
    assert first["error_code"] == "task_character_limit"
    again = await materials.parse_material(ARCH, UUID(source["material_version_id"]))
    assert again["job_id"] == first["job_id"]
    assert again["result_id"] is None


async def test_cancelled_build_is_never_reactivated(services):
    registry, _, base, _, _, version = await candidate(services)
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused(FakeEmbedding):
        async def embed(self, texts):
            entered.set()
            await release.wait()
            return await super().embed(texts)

    _, _, index, _ = components(services[0], embedding=Paused(base.embedding.outbound))
    pending = asyncio.create_task(index.build(ARCH, version))
    await entered.wait()
    async with registry.database.transaction() as conn:
        build_id = (
            await rows(conn, "SELECT id FROM presales.index_builds WHERE state='building'")
        )[0]["id"]
    await index.cancel(ARCH, build_id)
    release.set()
    result = await pending
    assert result["state"] == "cancelled"
    async with registry.database.transaction() as conn:
        assert not await rows(conn, "SELECT * FROM presales.knowledge_directory")


async def test_mid_build_revocation_fails_closed(services):
    _, _, base, _, document, version = await candidate(services)
    maintenance = services[1]

    class Revoking(FakeEmbedding):
        async def embed(self, texts):
            change = await maintenance.preview(
                [
                    AuthorizationChange(
                        kind="revoke", principal_id=ARCH.subject, resource_id=document
                    )
                ]
            )
            await maintenance.apply(
                UUID(change["change_id"]), change["digest"], "Synthetic mid-build restriction"
            )
            return await super().embed(texts)

    _, _, index, _ = components(services[0], embedding=Revoking(base.embedding.outbound))
    with pytest.raises(AccessDenied):
        await index.build(ARCH, version)
    async with services[2].transaction() as conn:
        assert (await rows(conn, "SELECT state FROM presales.index_builds"))[0]["state"] == "failed"
        assert not await rows(conn, "SELECT * FROM presales.knowledge_directory")


async def test_5000_candidate_chunk_boundary_without_truncation(services):
    registry, _, index, _, _, version = await candidate(services, text=b"x\n\n" * 5000)
    async with registry.database.transaction() as conn:
        policy = Policy(conn, ARCH)
        await policy.start(architect=True)
        record = await registry.version(conn, policy, version)
        await index._capacity(conn, record, 5000, None)
        with pytest.raises(InvalidInput):
            await index._capacity(conn, record, 5001, None)
    parsed_id, parsed = await index.materials.parsed_document(ARCH, version)
    assert len(split_chunks(parsed, parsed_id, ChunkConfig())) == 5000
    build = await index.build(ARCH, version)
    assert build["state"] == "ready"
    async with registry.database.transaction() as conn:
        assert (
            await rows(
                conn,
                "SELECT count(*) AS n FROM presales.embeddings WHERE build_id=:id",
                id=UUID(build["build_id"]),
            )
        )[0]["n"] == 5000


async def test_role_change_cannot_enable_self_approval(services):
    _, _, index, review, _, version = await candidate(services)
    build = await index.build(ARCH, version)
    submitted = await review.submit(ARCH, version, UUID(build["build_id"]), 1, 0, uuid4())
    change = await services[1].preview(
        [AuthorizationChange(kind="role", principal_id=ARCH.subject, role="reviewer")]
    )
    await services[1].apply(
        UUID(change["change_id"]),
        change["digest"],
        "Synthetic role transition",
        confirm_expansion=True,
    )
    with pytest.raises(AccessDenied):
        await review.decide(
            ARCH, UUID(submitted["review_id"]), True, "Self approval after role change", uuid4()
        )


async def test_audit_failure_rolls_back_entire_publication(services):
    registry, _, index, review, _, version = await candidate(services)
    build = await index.build(ARCH, version)
    submitted = await review.submit(ARCH, version, UUID(build["build_id"]), 1, 0, uuid4())
    admin = services[2]
    async with admin.transaction() as conn:
        await execute(conn, "REVOKE INSERT ON audit.events FROM presales_runtime")
    try:
        with pytest.raises(StorageUnavailable):
            await review.decide(
                REVIEWER, UUID(submitted["review_id"]), True, "Synthetic publication", uuid4()
            )
    finally:
        async with admin.transaction() as conn:
            await execute(conn, "GRANT INSERT ON audit.events TO presales_runtime")
    async with registry.database.transaction() as conn:
        assert not await rows(conn, "SELECT * FROM presales.knowledge_directory")
        assert not await rows(conn, "SELECT * FROM presales.knowledge_decisions")
        assert (await rows(conn, "SELECT revision FROM presales.knowledge_catalog"))[0][
            "revision"
        ] == 0
    assert (
        await review.decide(
            REVIEWER, UUID(submitted["review_id"]), True, "Synthetic recovery", uuid4()
        )
    )["state"] == "published"
