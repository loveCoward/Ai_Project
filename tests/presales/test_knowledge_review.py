from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import AccessDenied, Conflict
from tests.presales.phase2a_helpers import ARCH, REVIEWER, SALES, candidate

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_roles_rejection_resubmission_and_old_review_rejected(services):
    _, _, index, review, _, version = await candidate(services)
    build = await index.build(ARCH, version)
    with pytest.raises(AccessDenied):
        await review.submit(SALES, version, UUID(build["build_id"]), 1, 0, uuid4())
    first = await review.submit(ARCH, version, UUID(build["build_id"]), 1, 0, uuid4())
    with pytest.raises(AccessDenied):
        await review.decide(ARCH, UUID(first["review_id"]), True, "Self approval denied", uuid4())
    rejected = await review.decide(
        REVIEWER, UUID(first["review_id"]), False, "Needs another review", uuid4()
    )
    assert rejected["state"] == "candidate"
    second = await review.submit(ARCH, version, UUID(build["build_id"]), 3, 0, uuid4())
    with pytest.raises(Conflict):
        await review.decide(REVIEWER, UUID(first["review_id"]), True, "Stale review", uuid4())
    assert (
        await review.decide(
            REVIEWER, UUID(second["review_id"]), True, "Synthetic approval", uuid4()
        )
    )["state"] == "published"
