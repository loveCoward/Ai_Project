from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import AccessDenied, Conflict, NotReady, SourceDeclaration
from presales.indexing.embedding import EmbeddingFailure
from presales.infrastructure.database import rows
from presales.maintenance.service import AuthorizationChange
from presales.retrieval.service import RetrievalService
from tests.presales.phase2a_helpers import ARCH, REVIEWER, SALES, SCOPE, candidate, publish
from tests.presales.test_retrieval_units import query

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def restricted(services):
    registry, materials, index, review, document, version, build = await publish(services)
    return RetrievalService(registry, index.embedding), review, document, version, build


async def revoke(services, document):
    maintenance = services[1]
    change = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=SALES.subject, resource_id=document)]
    )
    await maintenance.apply(UUID(change["change_id"]), change["digest"], "Synthetic restriction")


async def test_dual_retrieval_cache_locations_and_tightened_replay(services):
    retrieval, _, document, version, _ = await restricted(services)
    result = await retrieval.retrieve_evidence(SALES, query(query="capability"))
    assert result.mode == "fake" and result.hits and not result.cache_hit
    assert result.hits[0].source_version_id == version
    identifier = result.hits[0].evidence_id
    assert (await retrieval.read_evidence(SALES, identifier)).text == result.hits[0].text
    replay = await retrieval.retrieve_evidence(SALES, query(query="capability"))
    assert replay.cache_hit and replay.hits == result.hits
    trace = await retrieval.read_run(SALES, result.run_id)
    assert trace["ranking"]["vector"] and trace["ranking"]["keyword"]
    await revoke(services, document)
    assert not (await retrieval.retrieve_evidence(SALES, query(query="capability"))).hits
    with pytest.raises(AccessDenied):
        await retrieval.read_evidence(SALES, identifier)
    with pytest.raises(AccessDenied):
        await retrieval.read_run(SALES, result.run_id)
    with pytest.raises(AccessDenied):
        await retrieval.read_run(REVIEWER, result.run_id)


async def test_retired_knowledge_disappears_but_history_remains_restricted(services):
    retrieval, review, document, version, build = await restricted(services)
    old = await retrieval.retrieve_evidence(SALES, query(query="capability"))
    retirement = await review.submit(ARCH, version, build, 3, 1, uuid4(), action="retire")
    await review.decide(
        REVIEWER, UUID(retirement["review_id"]), True, "Synthetic retirement", uuid4()
    )
    assert not (await retrieval.retrieve_evidence(SALES, query(query="capability"))).hits
    assert (
        await retrieval.read_evidence(SALES, old.hits[0].evidence_id)
    ).source_version_id == version
    await revoke(services, document)
    with pytest.raises(AccessDenied):
        await retrieval.read_evidence(SALES, old.hits[0].evidence_id)


async def test_candidate_conflict_and_model_mismatch_do_not_fall_back(services):
    retrieval, _, document, _, _ = await restricted(services)
    retrieval.embedding.model_identity = "changed-model"
    with pytest.raises(NotReady):
        await retrieval.retrieve_evidence(SALES, query())
    retrieval.embedding.model_identity = "deterministic-sha256-engineering-v1"
    await retrieval.registry.register_version(
        ARCH, document, "v2.md", b"synthetic pending", SourceDeclaration.SYNTHETIC, SCOPE, uuid4()
    )
    with pytest.raises(Conflict):
        await retrieval.retrieve_evidence(SALES, query())


async def test_unpublished_and_unreadable_blocks_never_reach_ranker(services):
    retrieval, _, _, _, _ = await restricted(services)
    registry, _, index, review, hidden_doc, hidden_version = await candidate(
        services, text=b"PRIVATE sentinel forbidden"
    )
    build = await index.build(ARCH, hidden_version)
    pending = await review.submit(ARCH, hidden_version, UUID(build["build_id"]), 1, 1, uuid4())
    await review.decide(
        REVIEWER, UUID(pending["review_id"]), True, "Synthetic hidden knowledge", uuid4()
    )
    await candidate(services, text=b"UNPUBLISHED must never reach ranking")
    await revoke(services, hidden_doc)
    seen = []

    class RecordingRanker:
        identity = "recording-engineering-ranker"

        def authorize(self, declaration):
            assert declaration == "synthetic"

        async def rank(self, text, documents):
            seen.extend(documents)
            return list(range(len(documents)))

    retrieval.reranker = RecordingRanker()
    result = await retrieval.retrieve_evidence(SALES, query(query="sentinel capability"))
    assert result.hits and all(v.source_version_id != hidden_version for v in result.hits)
    assert seen and not any("PRIVATE" in text or "UNPUBLISHED" in text for text in seen)


async def test_embedding_failure_and_reranker_failure_are_visible(services):
    retrieval, _, _, _, _ = await restricted(services)

    async def failed(_):
        raise EmbeddingFailure()

    retrieval.embedding.embed = failed

    class FailedRanker:
        identity = "failed-engineering-ranker"

        def authorize(self, _):
            pass

        async def rank(self, *_):
            raise RuntimeError("do not expose provider details")

    retrieval.reranker = FailedRanker()
    result = await retrieval.retrieve_evidence(SALES, query(query="capability"))
    assert result.hits and not result.cache_hit
    assert result.degradation == ["embedding_failed_keyword_only", "reranker_failed_rrf_retained"]
    assert not (await retrieval.retrieve_evidence(SALES, query(query="capability"))).cache_hit


async def test_revocation_during_rerank_refuses_return_and_trace(services):
    retrieval, _, document, _, _ = await restricted(services)

    class RevokingRanker:
        identity = "revoking-engineering-ranker"

        def authorize(self, _):
            pass

        async def rank(self, _, documents):
            await revoke(services, document)
            return list(range(len(documents)))

    retrieval.reranker = RevokingRanker()
    with pytest.raises(Conflict):
        await retrieval.retrieve_evidence(SALES, query())
    async with services[2].transaction() as conn:
        assert not await rows(conn, "SELECT id FROM presales.retrieval_runs")
