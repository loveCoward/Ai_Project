import asyncio
from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import AccessDenied, SourceDeclaration, StorageUnavailable
from presales.indexing.embedding import EmbeddingFailure, FakeEmbedding
from presales.infrastructure.database import execute, rows
from presales.maintenance.service import AuthorizationChange
from presales.materials.models import ManifestItem
from presales.materials.parser import parse_bytes
from presales.materials.service import MaterialService
from tests.presales.phase2a_helpers import ARCH, OPP, candidate, components

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_revocation_during_parse_records_failure_without_returning_text(services):
    service, maintenance, admin, _ = services
    source = await service.register_material(
        ARCH, OPP, "x.md", b"Synthetic restricted text", SourceDeclaration.SYNTHETIC, uuid4()
    )
    version = UUID(source["material_version_id"])
    entered, released = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    class PausedParser:
        def parse(self, filename, data):
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(released.wait(), loop).result(timeout=10)
            return parse_bytes(filename, data)

    materials = MaterialService(service, parser=PausedParser())
    pending = asyncio.create_task(materials.parse_material(ARCH, version))
    await entered.wait()
    change = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=ARCH.subject, resource_id=OPP)]
    )
    await maintenance.apply(
        UUID(change["change_id"]), change["digest"], "Synthetic revoke during parse"
    )
    released.set()
    with pytest.raises(AccessDenied):
        await pending
    async with admin.transaction() as conn:
        assert (await rows(conn, "SELECT state,error_code FROM presales.parse_jobs"))[0] == {
            "state": "failed",
            "error_code": "source_access_changed",
        }
        assert not await rows(conn, "SELECT * FROM presales.parse_results")
        assert (await rows(conn, "SELECT code FROM presales.parse_errors"))[0][
            "code"
        ] == "source_access_changed"


async def test_file_readiness_tightening_invalidates_previously_complete_inputs(services):
    service = services[0]
    task = await service.create_task(ARCH, OPP, "Synthetic integrity test", uuid4())
    task_id = UUID(task["resource_id"])
    material = await service.register_material(
        ARCH,
        task_id,
        "x.md",
        b"Synthetic ready source",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        expected_task_revision=1,
    )
    version = UUID(material["material_version_id"])
    materials = MaterialService(service)
    await materials.parse_material(ARCH, version)
    status = await materials.set_manifest(
        ARCH, task_id, 2, [ManifestItem(material_version_id=version, necessity="required")]
    )
    assert status["complete"]
    service.files.path(material["storage_key"]).write_bytes(b"corrupt")
    status = await materials.input_status(ARCH, task_id)
    assert not status["complete"] and status["unresolved"] == [str(version)]
    assert (await service.read_task(ARCH, task_id))["state"] == "awaiting_supplementation"


async def test_partial_embedding_batch_failure_and_total_deadline_are_explicit(services):
    registry, _, base, _, _, version = await candidate(services, text=b"first\n\nsecond")

    class Partial(FakeEmbedding):
        calls = 0

        async def embed(self, texts):
            self.calls += 1
            if self.calls > 1:
                raise EmbeddingFailure()
            return await super().embed(texts)

    fake = Partial(base.embedding.outbound)
    _, _, index, _ = components(services[0], embedding=fake, batch_size=1)
    failed = await index.build(ARCH, version)
    assert failed["state"] == "failed" and fake.calls == 4
    async with registry.database.transaction() as conn:
        assert not await rows(conn, "SELECT * FROM presales.chunks")
        assert not await rows(conn, "SELECT * FROM presales.knowledge_directory")

    class Slow(FakeEmbedding):
        async def embed(self, texts):
            await asyncio.sleep(0.05)
            return await super().embed(texts)

    _, _, limited, _ = components(
        services[0], embedding=Slow(base.embedding.outbound), batch_seconds=0.01
    )
    timed = await limited.build(ARCH, version)
    assert timed["state"] == "failed" and timed["error_code"] == "index_timeout"


async def test_runtime_vector_dimensions_and_ready_build_are_immutable(services):
    registry, _, index, _, _, version = await candidate(services)
    build = await index.build(ARCH, version)
    build_id = UUID(build["build_id"])
    for statement in (
        "UPDATE presales.index_builds SET state='building' WHERE id=:id",
        "UPDATE presales.embeddings SET dimensions=99 WHERE build_id=:id",
        "DELETE FROM presales.knowledge_versions WHERE id=:id",
    ):
        with pytest.raises(StorageUnavailable):
            async with registry.database.transaction() as conn:
                await execute(
                    conn,
                    statement,
                    id=build_id if "knowledge_versions" not in statement else version,
                )
    async with registry.database.transaction() as conn:
        assert (
            await rows(conn, "SELECT public.vector_dims(embedding) AS n FROM presales.embeddings")
        )[0]["n"] == 8
