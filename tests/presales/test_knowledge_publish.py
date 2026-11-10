import asyncio
from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import AccessDenied, Conflict, SourceDeclaration
from presales.infrastructure.database import rows
from presales.knowledge.models import Applicability
from presales.maintenance.service import AuthorizationChange
from tests.presales.phase2a_helpers import ARCH, REVIEWER, SALES, SCOPE, publish

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_publish_replace_retire_and_historical_access(services):
    registry, materials, index, review, document, old, old_build = await publish(services)
    assert (await registry.published(SALES, SCOPE))[0]["version_id"] == str(old)
    new = await registry.register_version(
        ARCH,
        document,
        "v2.md",
        b"Synthetic new capability",
        SourceDeclaration.SYNTHETIC,
        SCOPE,
        uuid4(),
    )
    version = UUID(new["version_id"])
    await materials.parse_material(ARCH, version)
    with pytest.raises(Conflict):
        await registry.published(SALES, SCOPE)
    build = await index.build(ARCH, version, replaces=old)
    submitted = await review.submit(
        ARCH, version, UUID(build["build_id"]), 1, 1, uuid4(), action="replace", replaces=old
    )
    assert (
        await review.decide(
            REVIEWER, UUID(submitted["review_id"]), True, "Replace synthetic version", uuid4()
        )
    )["state"] == "published"
    assert (await registry.inspect(ARCH, old))["state"] == "replaced"
    assert (await registry.published(SALES, SCOPE))[0]["version_id"] == str(version)
    retired = await review.submit(
        ARCH, version, UUID(build["build_id"]), 3, 2, uuid4(), action="retire"
    )
    assert (
        await review.decide(
            REVIEWER, UUID(retired["review_id"]), True, "Retire synthetic version", uuid4()
        )
    )["state"] == "retired"
    assert await registry.published(SALES, SCOPE) == []
    assert (await registry.inspect(SALES, old))["state"] == "replaced"
    service, maintenance, _, _ = services
    change = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=SALES.subject, resource_id=document)]
    )
    await maintenance.apply(UUID(change["change_id"]), change["digest"], "Synthetic restriction")
    with pytest.raises(AccessDenied):
        await registry.inspect(SALES, old)
    async with service.database.transaction() as conn:
        assert len(await rows(conn, "SELECT * FROM presales.knowledge_changes")) == 3


async def test_concurrent_replace_and_retire_cannot_both_win(services):
    registry, materials, index, review, document, old, old_build = await publish(services)
    new = await registry.register_version(
        ARCH,
        document,
        "v2.md",
        b"Synthetic replacement",
        SourceDeclaration.SYNTHETIC,
        SCOPE,
        uuid4(),
    )
    version = UUID(new["version_id"])
    await materials.parse_material(ARCH, version)
    build = await index.build(ARCH, version, replaces=old)
    replacement = await review.submit(
        ARCH, version, UUID(build["build_id"]), 1, 1, uuid4(), action="replace", replaces=old
    )
    retirement = await review.submit(ARCH, old, old_build, 3, 1, uuid4(), action="retire")
    results = await asyncio.gather(
        *(
            review.decide(
                REVIEWER, UUID(r["review_id"]), True, "Concurrent synthetic decision", uuid4()
            )
            for r in (replacement, retirement)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, Conflict) for r in results) == 1
    async with registry.database.transaction() as conn:
        assert len(await rows(conn, "SELECT * FROM presales.knowledge_directory")) <= 1


async def test_reviewer_scope_adjustment_resolves_conflict_and_invalidates_old_review(services):
    registry, materials, index, review, document, old, _ = await publish(services)
    new = await registry.register_version(
        ARCH,
        document,
        "branch.md",
        b"Synthetic cloud branch",
        SourceDeclaration.SYNTHETIC,
        SCOPE,
        uuid4(),
    )
    version = UUID(new["version_id"])
    await materials.parse_material(ARCH, version)
    build = await index.build(ARCH, version)
    stale = await review.submit(ARCH, version, UUID(build["build_id"]), 1, 1, uuid4())
    with pytest.raises(Conflict):
        await review.decide(
            REVIEWER, UUID(stale["review_id"]), True, "Unresolved scope conflict", uuid4()
        )
    cloud = Applicability(products=SCOPE.products, deployments=("cloud",))
    with pytest.raises(AccessDenied):
        await review.adjust_candidate_scope(
            ARCH, version, cloud, 2, 1, "Actor is not reviewer", uuid4()
        )
    adjusted = await review.adjust_candidate_scope(
        REVIEWER, version, cloud, 2, 1, "Synthetic branch correction", uuid4()
    )
    assert adjusted["revision"] == 3 and adjusted["catalog_revision"] == 2
    with pytest.raises(Conflict):
        await review.decide(REVIEWER, UUID(stale["review_id"]), True, "Old scope review", uuid4())
    current = await review.submit(ARCH, version, UUID(build["build_id"]), 3, 2, uuid4())
    await review.decide(
        REVIEWER, UUID(current["review_id"]), True, "Publish corrected branch", uuid4()
    )
    assert (await registry.published(SALES, SCOPE))[0]["version_id"] == str(old)
    assert (await registry.published(SALES, cloud))[0]["version_id"] == str(version)
    with pytest.raises(Conflict):
        await review.adjust_candidate_scope(
            REVIEWER, version, SCOPE, 5, 3, "Published scope is immutable", uuid4()
        )
