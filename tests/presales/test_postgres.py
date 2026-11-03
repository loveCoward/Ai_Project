import asyncio
from uuid import UUID, uuid4

import pytest

from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    DraftContent,
    InvalidInput,
    NotReady,
    SourceDeclaration,
    StorageUnavailable,
)
from presales.infrastructure.database import AUTH_LOCK, execute, lock, rows
from presales.maintenance.service import AuthorizationChange, demo_id

pytestmark = pytest.mark.postgres
ARCH = Actor(subject=demo_id("architect"))
SALES = Actor(subject=demo_id("sales"))
REVIEWER = Actor(subject=demo_id("reviewer"))
OPP = demo_id("opportunity-a")


async def task_and_draft(service):
    task = await service.create_task(ARCH, OPP, "Synthetic proposal", uuid4())
    draft = await service.create_draft(
        ARCH, UUID(task["resource_id"]), 1, uuid4(), DraftContent(title="Synthetic draft")
    )
    return task, draft


@pytest.mark.asyncio
async def test_roundtrip_and_immutable_history(services):
    service, _, admin, _ = services
    task, draft = await task_and_draft(service)
    revised = await service.save_draft(
        ARCH, UUID(draft["resource_id"]), 2, 1, uuid4(), DraftContent(title="Edited")
    )
    old = await service.read_draft(SALES, UUID(draft["source_version_id"]))
    new = await service.read_draft(REVIEWER, UUID(revised["source_version_id"]))
    assert old["draft"]["title"] == "Synthetic draft"
    assert new["identity"]["revision"] == 2
    assert draft["source_version_id"] in new["access"]["direct_source_version_ids"]
    assert new["identity"]["analysis_attempt_id"] is None
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.proposal_revisions SET body='{}' WHERE id=:id",
                id=UUID(draft["source_version_id"]),
            )


@pytest.mark.asyncio
async def test_roles_and_cross_opportunity(services):
    service, _, _, _ = services
    for actor, opportunity in [(SALES, OPP), (REVIEWER, OPP), (ARCH, demo_id("opportunity-b"))]:
        with pytest.raises(AccessDenied):
            await service.create_task(actor, opportunity, "Blocked", uuid4())


@pytest.mark.asyncio
async def test_idempotency_before_revision_and_parameter_conflict(services):
    service, _, _, _ = services
    task, draft = await task_and_draft(service)
    request = uuid4()
    content = DraftContent(title="Retry safely")
    first = await service.save_draft(ARCH, UUID(draft["resource_id"]), 2, 1, request, content)
    assert first == await service.save_draft(
        ARCH, UUID(draft["resource_id"]), 2, 1, request, content
    )
    with pytest.raises(Conflict):
        await service.save_draft(
            ARCH, UUID(draft["resource_id"]), 2, 1, request, DraftContent(title="Other")
        )


@pytest.mark.asyncio
async def test_concurrent_distinct_and_identical_commands(services):
    service, _, _, _ = services
    _, draft = await task_and_draft(service)
    calls = [
        service.save_draft(
            ARCH, UUID(draft["resource_id"]), 2, 1, uuid4(), DraftContent(title=str(i))
        )
        for i in range(2)
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, Conflict) for r in results) == 1
    request = uuid4()
    calls = [service.create_task(ARCH, OPP, "Same request", request) for _ in range(2)]
    a, b = await asyncio.gather(*calls)
    assert a == b


@pytest.mark.asyncio
async def test_revocation_blocks_receipt_and_old_draft(services):
    service, maintenance, _, initializer = services
    request = uuid4()
    task = await service.create_task(ARCH, OPP, "Synthetic", request)
    draft = await service.create_draft(
        ARCH, UUID(task["resource_id"]), 1, uuid4(), DraftContent(title="Draft")
    )
    preview = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=ARCH.subject, resource_id=OPP)]
    )
    await maintenance.apply(
        UUID(preview["change_id"]), preview["digest"], "Synthetic revocation test"
    )
    assert (await initializer.initialize_demo())["status"] == "already_initialized"
    with pytest.raises(AccessDenied):
        await service.create_task(ARCH, OPP, "Synthetic", request)
    with pytest.raises(AccessDenied):
        await service.read_draft(ARCH, UUID(draft["source_version_id"]))


@pytest.mark.asyncio
async def test_maintenance_preview_stale_expansion_and_retry(services):
    _, maintenance, _, _ = services
    grant = AuthorizationChange(
        kind="grant", principal_id=ARCH.subject, resource_id=demo_id("opportunity-b")
    )
    first = await maintenance.preview([grant])
    stale = await maintenance.preview([grant])
    with pytest.raises(AccessDenied):
        await maintenance.apply(UUID(first["change_id"]), first["digest"], "test")
    applied = await maintenance.apply(
        UUID(first["change_id"]), first["digest"], "test", confirm_expansion=True
    )
    assert applied == await maintenance.apply(
        UUID(first["change_id"]), first["digest"], "test", confirm_expansion=True
    )
    with pytest.raises(Conflict):
        await maintenance.apply(
            UUID(stale["change_id"]), stale["digest"], "test", confirm_expansion=True
        )
    assert (await maintenance.status(UUID(first["change_id"])))["applied_revision"] == applied[
        "revision"
    ]


@pytest.mark.asyncio
async def test_files_ready_unknown_sensitive_corrupt(services):
    service, _, _, _ = services
    material = await service.register_material(
        ARCH, OPP, "synthetic.txt", b"synthetic", SourceDeclaration.SYNTHETIC, uuid4()
    )
    identifier = UUID(material["material_version_id"])
    assert await service.read_material(SALES, identifier) == b"synthetic"
    service.files.path(material["storage_key"]).write_bytes(b"corrupt")
    with pytest.raises(NotReady):
        await service.read_material(SALES, identifier)
    unknown = await service.register_material(
        ARCH, OPP, "unknown.txt", b"unknown", SourceDeclaration.UNKNOWN, uuid4()
    )
    with pytest.raises(AccessDenied):
        await service.read_material(SALES, UUID(unknown["material_version_id"]))
    with pytest.raises(InvalidInput):
        await service.register_material(
            ARCH, OPP, "sensitive.txt", b"synthetic test only", SourceDeclaration.SENSITIVE, uuid4()
        )


@pytest.mark.asyncio
async def test_file_written_database_finalization_fails_then_reconciles(services, monkeypatch):
    service, _, admin, _ = services
    original = service.reconcile_material

    async def fail(*args):
        raise StorageUnavailable()

    monkeypatch.setattr(service, "reconcile_material", fail)
    request = uuid4()
    with pytest.raises(StorageUnavailable):
        await service.register_material(
            ARCH, OPP, "synthetic.txt", b"synthetic", SourceDeclaration.SYNTHETIC, request
        )
    async with admin.transaction() as conn:
        values = await rows(conn, "SELECT id FROM presales.material_versions")
    assert len(values) == 1
    with pytest.raises(AccessDenied):
        await service.read_material(SALES, values[0]["id"])
    monkeypatch.setattr(service, "reconcile_material", original)
    await service.register_material(
        ARCH, OPP, "synthetic.txt", b"synthetic", SourceDeclaration.SYNTHETIC, request
    )
    assert await service.read_material(SALES, values[0]["id"]) == b"synthetic"


@pytest.mark.asyncio
async def test_task_only_grant_does_not_grant_parent_or_sibling(services):
    service, maintenance, _, _ = services
    a = await service.create_task(ARCH, OPP, "A", uuid4())
    b = await service.create_task(ARCH, OPP, "B", uuid4())
    changes = [
        AuthorizationChange(kind="revoke", principal_id=SALES.subject, resource_id=OPP),
        AuthorizationChange(
            kind="grant", principal_id=SALES.subject, resource_id=UUID(a["resource_id"])
        ),
    ]
    preview = await maintenance.preview(changes)
    await maintenance.apply(
        UUID(preview["change_id"]), preview["digest"], "test", confirm_expansion=True
    )
    assert (await service.read_task(SALES, UUID(a["resource_id"])))["goal"] == "A"
    with pytest.raises(AccessDenied):
        await service.read_task(SALES, UUID(b["resource_id"]))


@pytest.mark.asyncio
async def test_runtime_cannot_ddl_maintain_authorization_or_edit_audit(services):
    service, maintenance, _, _ = services
    for sql in [
        "UPDATE iam.principals SET active=false",
        "CREATE TABLE presales.forbidden(id int)",
        "DELETE FROM audit.events",
        "UPDATE audit.events SET result='changed'",
        "UPDATE presales.tasks SET state='approved'",
    ]:
        with pytest.raises(StorageUnavailable):
            async with service.database.transaction() as conn:
                await execute(conn, sql)
    with pytest.raises(StorageUnavailable):
        async with maintenance.database.transaction() as conn:
            await execute(conn, "UPDATE presales.tasks SET goal='changed'")


@pytest.mark.asyncio
async def test_authorization_lock_orders_revocation(services):
    service, maintenance, _, _ = services
    preview = await maintenance.preview(
        [AuthorizationChange(kind="active", principal_id=ARCH.subject, active=False)]
    )
    started = asyncio.Event()

    async def revoke():
        started.set()
        return await maintenance.apply(UUID(preview["change_id"]), preview["digest"], "test")

    async with service.database.transaction() as conn:
        await lock(conn, AUTH_LOCK, shared=True)
        pending = asyncio.create_task(revoke())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not pending.done()
    await pending
    with pytest.raises(AccessDenied):
        await service.create_task(ARCH, OPP, "Blocked", uuid4())


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_business_write(services):
    service, _, admin, _ = services
    async with admin.transaction() as conn:
        await execute(conn, "REVOKE INSERT ON audit.events FROM presales_runtime")
    try:
        with pytest.raises(StorageUnavailable):
            await service.create_task(ARCH, OPP, "Must rollback", uuid4())
        async with admin.transaction() as conn:
            assert not await rows(conn, "SELECT id FROM presales.tasks")
    finally:
        async with admin.transaction() as conn:
            await execute(conn, "GRANT INSERT ON audit.events TO presales_runtime")


@pytest.mark.asyncio
async def test_actual_sources_propagate_when_display_citations_removed(services):
    service, maintenance, admin, _ = services
    knowledge, source = uuid4(), uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn, "INSERT INTO presales.resources VALUES(:id,'knowledge',NULL)", id=knowledge
        )
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,created_by)
            VALUES(:id,:resource,1,'original','synthetic',true,true,:actor)""",
            id=source,
            resource=knowledge,
            actor=ARCH.subject,
        )
    preview = await maintenance.preview(
        [AuthorizationChange(kind="grant", principal_id=ARCH.subject, resource_id=knowledge)]
    )
    await maintenance.apply(
        UUID(preview["change_id"]),
        preview["digest"],
        "Synthetic restricted source",
        confirm_expansion=True,
    )
    task = await service.create_task(ARCH, OPP, "Synthetic task", uuid4())
    draft = await service.create_draft(
        ARCH,
        UUID(task["resource_id"]),
        1,
        uuid4(),
        DraftContent(title="Restricted", evidence_version_ids=[source]),
    )
    edited = await service.save_draft(
        ARCH, UUID(draft["resource_id"]), 2, 1, uuid4(), DraftContent(title="No visible citations")
    )
    with pytest.raises(AccessDenied):
        await service.read_draft(SALES, UUID(edited["source_version_id"]))
    revoke = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=ARCH.subject, resource_id=knowledge)]
    )
    await maintenance.apply(UUID(revoke["change_id"]), revoke["digest"], "Revoke synthetic source")
    with pytest.raises(AccessDenied):
        await service.read_draft(ARCH, UUID(edited["source_version_id"]))


@pytest.mark.asyncio
async def test_cycles_and_missing_manifests_are_rejected(services):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic", uuid4())
    a, b = uuid4(), uuid4()
    async with admin.transaction() as conn:
        for identifier, number in [(a, 2), (b, 3)]:
            await execute(
                conn,
                """INSERT INTO presales.resource_versions
                (id,resource_id,version_no,source_kind,created_by)
                VALUES(:id,:resource,:number,'derived',:actor)""",
                id=identifier,
                resource=UUID(task["resource_id"]),
                number=number,
                actor=ARCH.subject,
            )
        await execute(
            conn, "INSERT INTO presales.direct_dependencies VALUES(:a,:b,'test')", a=a, b=b
        )
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn, "INSERT INTO presales.direct_dependencies VALUES(:b,:a,'test')", a=a, b=b
            )
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.resource_versions SET ready=true,dependencies_complete=true WHERE id=:id",
                id=a,
            )
    with pytest.raises(AccessDenied):
        await service.read_draft(ARCH, a)


@pytest.mark.asyncio
async def test_concurrent_opposing_source_edges_cannot_form_cycle(services):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic", uuid4())
    a, b = uuid4(), uuid4()
    async with admin.transaction() as conn:
        for identifier, number in [(a, 2), (b, 3)]:
            await execute(
                conn,
                """INSERT INTO presales.resource_versions
                (id,resource_id,version_no,source_kind,created_by)
                VALUES(:id,:resource,:number,'derived',:actor)""",
                id=identifier,
                resource=UUID(task["resource_id"]),
                number=number,
                actor=ARCH.subject,
            )

    async def edge(downstream, upstream):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO presales.direct_dependencies VALUES(:a,:b,'test')",
                a=downstream,
                b=upstream,
            )
        return True

    results = await asyncio.gather(edge(a, b), edge(b, a), return_exceptions=True)
    assert results.count(True) == 1
    assert sum(isinstance(r, StorageUnavailable) for r in results) == 1


@pytest.mark.asyncio
async def test_task_input_not_ready_prevents_draft(services):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic", uuid4())
    material = await service.register_material(
        ARCH,
        UUID(task["resource_id"]),
        "unknown.txt",
        b"unknown",
        SourceDeclaration.UNKNOWN,
        uuid4(),
        expected_task_revision=1,
    )
    assert material["revision"] == 2
    with pytest.raises(AccessDenied):
        await service.create_draft(
            ARCH, UUID(task["resource_id"]), 2, uuid4(), DraftContent(title="Blocked")
        )


@pytest.mark.asyncio
async def test_maintenance_audit_failure_rolls_back_and_retries(services):
    _, maintenance, admin, _ = services
    preview = await maintenance.preview(
        [AuthorizationChange(kind="active", principal_id=ARCH.subject, active=False)]
    )
    async with admin.transaction() as conn:
        await execute(conn, "REVOKE INSERT ON audit.events FROM presales_maintainer")
    try:
        with pytest.raises(StorageUnavailable):
            await maintenance.apply(
                UUID(preview["change_id"]), preview["digest"], "Synthetic reason"
            )
        async with admin.transaction() as conn:
            assert (
                await rows(conn, "SELECT active FROM iam.principals WHERE id=:id", id=ARCH.subject)
            )[0]["active"]
            assert (await rows(conn, "SELECT revision FROM iam.authorization_revision"))[0][
                "revision"
            ] == preview["base_revision"]
    finally:
        async with admin.transaction() as conn:
            await execute(conn, "GRANT INSERT ON audit.events TO presales_maintainer")
    await maintenance.apply(UUID(preview["change_id"]), preview["digest"], "Synthetic reason")


@pytest.mark.asyncio
async def test_task_owner_and_current_revision_foreign_keys(services):
    service, _, admin, _ = services
    task, draft = await task_and_draft(service)
    second = await service.create_task(ARCH, OPP, "Second", uuid4())
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.tasks SET candidate_version_id=:candidate WHERE id=:id",
                candidate=UUID(draft["resource_id"]),
                id=UUID(second["resource_id"]),
            )
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.tasks SET opportunity_id=:other WHERE id=:id",
                other=demo_id("opportunity-b"),
                id=UUID(task["resource_id"]),
            )


@pytest.mark.asyncio
async def test_detected_corrupt_material_blocks_derived_read(services):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic", uuid4())
    material = await service.register_material(
        ARCH,
        UUID(task["resource_id"]),
        "synthetic.txt",
        b"synthetic",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        expected_task_revision=1,
    )
    draft = await service.create_draft(
        ARCH, UUID(task["resource_id"]), 2, uuid4(), DraftContent(title="Draft")
    )
    service.files.path(material["storage_key"]).write_bytes(b"corrupt")
    with pytest.raises(NotReady):
        await service.read_material(SALES, UUID(material["material_version_id"]))
    with pytest.raises(AccessDenied):
        await service.read_draft(SALES, UUID(draft["source_version_id"]))


@pytest.mark.asyncio
async def test_tampered_preview_cannot_be_applied(services):
    _, maintenance, admin, _ = services
    preview = await maintenance.preview(
        [AuthorizationChange(kind="active", principal_id=ARCH.subject, active=False)]
    )
    async with admin.transaction() as conn:
        await execute(
            conn,
            "UPDATE iam.authorization_changes SET changes='[]' WHERE id=:id",
            id=UUID(preview["change_id"]),
        )
    with pytest.raises(Conflict):
        await maintenance.apply(UUID(preview["change_id"]), preview["digest"], "Synthetic reason")


@pytest.mark.asyncio
async def test_approved_and_review_drafts_cannot_be_edited(services):
    service, _, admin, _ = services
    _, draft = await task_and_draft(service)
    for state in ["awaiting_review", "approved", "rejected", "retired"]:
        async with admin.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.proposal_versions SET state=:state WHERE id=:id",
                state=state,
                id=UUID(draft["resource_id"]),
            )
        with pytest.raises(Conflict):
            await service.save_draft(
                ARCH, UUID(draft["resource_id"]), 2, 1, uuid4(), DraftContent(title="Blocked")
            )


@pytest.mark.asyncio
async def test_sensitive_payload_is_not_copied_into_audit_or_receipts(services):
    service, _, admin, _ = services
    marker = "synthetic-private-body-marker"
    task = await service.create_task(ARCH, OPP, marker, uuid4())
    await service.create_draft(
        ARCH, UUID(task["resource_id"]), 1, uuid4(), DraftContent(title=marker, notes=marker)
    )
    async with admin.transaction() as conn:
        events = await rows(conn, "SELECT row_to_json(e)::text AS body FROM audit.events e")
        receipts = await rows(
            conn, "SELECT row_to_json(r)::text AS body FROM presales.command_receipts r"
        )
        assert all(marker not in item["body"] for item in events + receipts)


@pytest.mark.asyncio
async def test_customer_material_from_another_task_cannot_enter_draft(services):
    service, _, _, _ = services
    first = await service.create_task(ARCH, OPP, "First synthetic task", uuid4())
    second = await service.create_task(ARCH, OPP, "Second synthetic task", uuid4())
    other = await service.register_material(
        ARCH,
        UUID(second["resource_id"]),
        "other.txt",
        b"synthetic other task material",
        SourceDeclaration.SYNTHETIC,
        uuid4(),
        expected_task_revision=1,
    )
    with pytest.raises(AccessDenied):
        await service.create_draft(
            ARCH,
            UUID(first["resource_id"]),
            1,
            uuid4(),
            DraftContent(
                title="Reject cross-task material",
                evidence_version_ids=[UUID(other["material_version_id"])],
            ),
        )
