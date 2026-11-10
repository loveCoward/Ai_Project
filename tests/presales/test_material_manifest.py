from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import AccessDenied, SourceDeclaration
from presales.materials.models import ManifestItem, ParseFailure
from presales.materials.service import MaterialService
from tests.presales.phase2a_helpers import ARCH, OPP, SALES

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


@pytest.mark.parametrize(
    "necessity,complete", [("required", False), ("optional", True), ("unclassified", False)]
)
async def test_manifest_failed_classification_and_success_immutability(
    services, necessity, complete
):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic input", uuid4())
    task_id = UUID(task["resource_id"])
    bad = await service.register_material(
        ARCH,
        task_id,
        "unsupported.txt",
        b"Synthetic",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        expected_task_revision=1,
    )
    materials = MaterialService(service)
    source = UUID(bad["material_version_id"])
    await materials.set_manifest(
        ARCH, task_id, 2, [ManifestItem(material_version_id=source, necessity=necessity)]
    )
    result = await materials.parse_material(ARCH, source)
    assert result["state"] == "failed"
    retry = await materials.parse_material(ARCH, source)
    assert result["job_id"] == retry["job_id"]
    status = await materials.input_status(ARCH, task_id)
    assert status["complete"] == complete
    assert bool(status["unused_sources"]) == (necessity == "optional")
    assert (await service.read_task(ARCH, task_id))["state"] == (
        "new" if complete else "awaiting_supplementation"
    )
    with pytest.raises(AccessDenied):
        await materials.parse_material(SALES, source)


async def test_parse_success_replay_and_revocation(services):
    from presales.maintenance.service import AuthorizationChange

    service, maintenance, _, _ = services
    material = await service.register_material(
        ARCH, OPP, "x.md", b"Synthetic source", SourceDeclaration.SYNTHETIC, uuid4()
    )
    source = UUID(material["material_version_id"])
    materials = MaterialService(service)
    first = await materials.parse_material(ARCH, source)
    assert first["state"] == "succeeded"
    assert first == await materials.parse_material(ARCH, source)
    change = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=ARCH.subject, resource_id=OPP)]
    )
    await maintenance.apply(UUID(change["change_id"]), change["digest"], "Synthetic revoke")
    with pytest.raises(AccessDenied):
        await materials.get_parse_result(ARCH, UUID(first["job_id"]))
    with pytest.raises(AccessDenied):
        await materials.parse_material(ARCH, source)


async def test_timeout_result_survives_retry_and_cannot_overwrite_success(services):
    service = services[0]
    material = await service.register_material(
        ARCH, OPP, "x.md", b"Synthetic timeout source", SourceDeclaration.SYNTHETIC, uuid4()
    )
    source = UUID(material["material_version_id"])

    class TimeoutParser:
        def parse(self, filename, data):
            raise ParseFailure("parse_timeout")

    materials = MaterialService(service, parser=TimeoutParser())
    failure = await materials.parse_material(ARCH, source)
    assert failure["state"] == "failed" and failure["error_code"] == "parse_timeout"
    materials.parser = MaterialService(service).parser
    success = await materials.parse_material(ARCH, source)
    assert success["job_id"] == failure["job_id"] and success["state"] == "succeeded"
    materials.parser = TimeoutParser()
    assert await materials.parse_material(ARCH, source) == success
    async with service.database.transaction() as conn:
        from presales.infrastructure.database import rows

        assert (
            len(
                await rows(
                    conn,
                    "SELECT * FROM presales.parse_errors WHERE job_id=:id",
                    id=UUID(failure["job_id"]),
                )
            )
            == 1
        )


async def test_adding_input_keeps_previous_classification_and_defaults_new_input_to_unclassified(
    services,
):
    service = services[0]
    materials = MaterialService(service)
    task = await service.create_task(ARCH, OPP, "Synthetic manifest revisions", uuid4())
    task_id = UUID(task["resource_id"])
    first = await service.register_material(
        ARCH,
        task_id,
        "first.txt",
        b"Synthetic optional",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        expected_task_revision=1,
    )
    await materials.set_manifest(
        ARCH,
        task_id,
        2,
        [
            ManifestItem(
                material_version_id=UUID(first["material_version_id"]), necessity="optional"
            )
        ],
    )
    second = await service.register_material(
        ARCH,
        task_id,
        "second.txt",
        b"Synthetic unclassified",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        expected_task_revision=3,
    )
    await materials.parse_material(ARCH, UUID(first["material_version_id"]))
    await materials.parse_material(ARCH, UUID(second["material_version_id"]))
    status = await materials.input_status(ARCH, task_id)
    assert status["unused_sources"] == [first["material_version_id"]]
    assert status["unresolved"] == [second["material_version_id"]]
