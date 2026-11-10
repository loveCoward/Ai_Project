from uuid import UUID, uuid4

import pytest

from presales.indexing.chunks import ChunkConfig, split_chunks
from presales.indexing.embedding import EmbeddingFailure, FakeEmbedding
from presales.infrastructure.database import rows
from presales.materials.parser import parse_bytes
from tests.presales.phase2a_helpers import ARCH, SCOPE, candidate, components


def test_chunks_are_stable_located_and_overlap_within_units():
    parsed = parse_bytes("x.md", ("中" * 1200 + "\n\n" + "文" * 300).encode())
    identifier = uuid4()
    config = ChunkConfig()
    chunks = split_chunks(parsed, identifier, config)
    assert chunks == split_chunks(parsed, identifier, config)
    assert len(chunks[0].tokens) == 600
    assert chunks[1].start == 520
    assert chunks[-1].location.paragraph == 2
    assert all(not ("中" in c.text and "文" in c.text) for c in chunks)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_ready_build_is_not_published_and_native_vectors_match(services):
    registry, _, index, _, _, version = await candidate(services)
    first = await index.build(ARCH, version)
    second = await index.build(ARCH, version)
    assert first["state"] == second["state"] == "ready"
    assert first["mode"] == "fake"
    assert await registry.published(ARCH, SCOPE) == []
    async with registry.database.transaction() as conn:
        assert (await rows(conn, "SELECT count(*) AS n FROM presales.knowledge_versions"))[0][
            "n"
        ] == 1
        assert (
            await rows(
                conn, "SELECT public.vector_dims(embedding) AS n FROM presales.embeddings LIMIT 1"
            )
        )[0]["n"] == 8


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("failure", ["technical", "dimensions", "timeout"])
async def test_embedding_failures_never_activate_partial_build(services, failure):
    import asyncio

    registry, materials, base, _, _, version = await candidate(services)

    class Broken(FakeEmbedding):
        calls = 0

        async def embed(self, texts):
            self.calls += 1
            if failure == "technical":
                raise EmbeddingFailure()
            if failure == "timeout":
                await asyncio.sleep(0.05)
            return [[1.0] for _ in texts]

    broken = Broken(base.embedding.outbound)
    _, _, index, _ = components(
        services[0], embedding=broken, request_seconds=0.001 if failure == "timeout" else 60
    )
    result = await index.build(ARCH, version)
    assert result["state"] == "failed"
    assert broken.calls == (1 if failure == "dimensions" else 3)
    async with registry.database.transaction() as conn:
        assert not await rows(conn, "SELECT * FROM presales.knowledge_directory")
        assert not await rows(
            conn, "SELECT * FROM presales.chunks WHERE build_id=:id", id=UUID(result["build_id"])
        )
