import asyncio
from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import AccessDenied, Conflict, InvalidInput, SourceDeclaration
from presales.infrastructure.database import rows
from presales.maintenance.service import AuthorizationChange
from tests.presales.phase2a_helpers import ARCH, REVIEWER, SCOPE, candidate, publish

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_replacement_net_count_and_concurrent_publication_capacity(services):
    registry, materials, index, review, document, version = await candidate(
        services, text=b"x\n\n" * 4998
    )
    large = await index.build(ARCH, version)
    assert large["state"] == "ready"
    submitted = await review.submit(ARCH, version, UUID(large["build_id"]), 1, 0, uuid4())
    await review.decide(
        REVIEWER, UUID(submitted["review_id"]), True, "Synthetic 4998-block catalog", uuid4()
    )
    new = await registry.register_version(
        ARCH,
        document,
        "replacement.md",
        b"new\n\n" * 4999,
        SourceDeclaration.SYNTHETIC,
        SCOPE,
        uuid4(),
    )
    replacement_id = UUID(new["version_id"])
    await materials.parse_material(ARCH, replacement_id)
    replaced = await index.build(ARCH, replacement_id, replaces=version)
    assert replaced["state"] == "ready"
    replacement = await review.submit(
        ARCH,
        replacement_id,
        UUID(replaced["build_id"]),
        1,
        1,
        uuid4(),
        action="replace",
        replaces=version,
    )
    await review.decide(
        REVIEWER, UUID(replacement["review_id"]), True, "Synthetic net replacement count", uuid4()
    )
    a = await candidate(services, text=b"one")
    b = await candidate(services, text=b"two")
    attempts = []
    for item in (a, b):
        build = await item[2].build(ARCH, item[5])
        attempts.append(await review.submit(ARCH, item[5], UUID(build["build_id"]), 1, 2, uuid4()))
    results = await asyncio.gather(
        *(
            review.decide(
                REVIEWER, UUID(r["review_id"]), True, "Concurrent capacity boundary", uuid4()
            )
            for r in attempts
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, Conflict) for r in results) == 1
    async with registry.database.transaction() as conn:
        assert (
            await rows(
                conn,
                "SELECT sum(b.chunk_count) AS total FROM presales.knowledge_directory d JOIN presales.index_builds b ON b.id=d.build_id",
            )
        )[0]["total"] == 5000
    c = await candidate(services, text=b"overflow")
    with pytest.raises(InvalidInput):
        await c[2].build(ARCH, c[5])
    async with registry.database.transaction() as conn:
        assert (
            await rows(
                conn,
                "SELECT sum(b.chunk_count) AS total FROM presales.knowledge_directory d JOIN presales.index_builds b ON b.id=d.build_id",
            )
        )[0]["total"] == 5000


async def test_decision_receipt_rechecks_current_authorization(services):
    registry, _, index, review, document, version = await candidate(services)
    build = await index.build(ARCH, version)
    submitted = await review.submit(ARCH, version, UUID(build["build_id"]), 1, 0, uuid4())
    request = uuid4()
    first = await review.decide(
        REVIEWER, UUID(submitted["review_id"]), True, "Synthetic replay", request
    )
    assert first == await review.decide(
        REVIEWER, UUID(submitted["review_id"]), True, "Synthetic replay", request
    )
    change = await services[1].preview(
        [AuthorizationChange(kind="revoke", principal_id=REVIEWER.subject, resource_id=document)]
    )
    await services[1].apply(
        UUID(change["change_id"]), change["digest"], "Synthetic revoke reviewer"
    )
    with pytest.raises(AccessDenied):
        await review.decide(
            REVIEWER, UUID(submitted["review_id"]), True, "Synthetic replay", request
        )


async def test_withdrawn_or_stale_review_cannot_replace_published_directory(services):
    registry, materials, index, review, document, version, old_build = await publish(services)
    new = await registry.register_version(
        ARCH, document, "candidate.md", b"candidate", SourceDeclaration.SYNTHETIC, SCOPE, uuid4()
    )
    replacement_id = UUID(new["version_id"])
    await materials.parse_material(ARCH, replacement_id)
    build = await index.build(ARCH, replacement_id, replaces=version)
    submitted = await review.submit(
        ARCH,
        replacement_id,
        UUID(build["build_id"]),
        1,
        1,
        uuid4(),
        action="replace",
        replaces=version,
    )
    await review.decide(
        REVIEWER, UUID(submitted["review_id"]), False, "Synthetic rejection", uuid4()
    )
    with pytest.raises(Conflict):
        await review.decide(
            REVIEWER, UUID(submitted["review_id"]), True, "Old review retry", uuid4()
        )
    async with registry.database.transaction() as conn:
        assert (
            await rows(
                conn,
                "SELECT build_id FROM presales.knowledge_directory WHERE version_id=:id",
                id=version,
            )
        )[0]["build_id"] == old_build
