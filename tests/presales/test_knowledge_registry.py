from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import (
    AccessDenied,
    Conflict,
    InvalidInput,
    SourceDeclaration,
    StorageUnavailable,
)
from presales.infrastructure.database import execute, rows
from tests.presales.phase2a_helpers import ARCH, SALES, SCOPE, candidate

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_candidate_scope_conflicts_and_immutable_history(services):
    registry, _, _, _, document, version = await candidate(services)
    assert await registry.published(SALES, SCOPE) == []
    with pytest.raises(AccessDenied):
        await registry.register_version(
            SALES, document, "x.md", b"synthetic", SourceDeclaration.SYNTHETIC, SCOPE, uuid4()
        )
    another = await registry.register_version(
        ARCH, document, "x.md", b"another synthetic", SourceDeclaration.SYNTHETIC, SCOPE, uuid4()
    )
    assert str(version) in (await registry.inspect(ARCH, UUID(another["version_id"])))["conflicts"]
    _, _, admin, _ = services
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.knowledge_versions SET filename='tampered' WHERE id=:id",
                id=version,
            )


async def test_version_101_rejected_and_receipt_does_not_recount(services):
    registry, _, _, _, document, version = await candidate(services)
    request = uuid4()
    first = await registry.register_version(
        ARCH, document, "x.md", b"synthetic", SourceDeclaration.SYNTHETIC, SCOPE, request
    )
    assert first == await registry.register_version(
        ARCH, document, "x.md", b"synthetic", SourceDeclaration.SYNTHETIC, SCOPE, request
    )
    for i in range(98):
        await registry.register_version(
            ARCH,
            document,
            "x.md",
            f"Synthetic {i}".encode(),
            SourceDeclaration.SYNTHETIC,
            SCOPE,
            uuid4(),
        )
    with pytest.raises(InvalidInput):
        await registry.register_version(
            ARCH, document, "x.md", b"101st", SourceDeclaration.SYNTHETIC, SCOPE, uuid4()
        )
    async with registry.database.transaction() as conn:
        assert (await rows(conn, "SELECT count(*) AS count FROM presales.knowledge_versions"))[0][
            "count"
        ] == 100


async def test_request_parameter_mismatch_is_conflict(services):
    registry, _, _, _, document, _ = await candidate(services)
    request = uuid4()
    await registry.register_version(
        ARCH, document, "x.md", b"synthetic", SourceDeclaration.SYNTHETIC, SCOPE, request
    )
    with pytest.raises(Conflict):
        await registry.register_version(
            ARCH, document, "x.md", b"different", SourceDeclaration.SYNTHETIC, SCOPE, request
        )
