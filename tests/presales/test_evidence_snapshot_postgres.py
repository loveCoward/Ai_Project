"""Real PostgreSQL checks for Evidence Snapshot immutability, permissions and fencing."""

from uuid import uuid4

import pytest

from presales.application.evidence_snapshot import EvidenceSnapshotService
from presales.application.proposal_attempt_context import ProposalAttemptContext
from presales.domain.contracts import StorageUnavailable
from presales.domain.evidence_snapshot import (
    NodeExecutionIdentity,
    SourceReference,
)
from presales.domain.proposal_analysis_attempt import AttemptIdentity
from presales.infrastructure.database import execute, rows
from presales.maintenance.service import demo_id

BUDGET = (
    '{"max_active_seconds":600,"max_model_seconds":120,"max_mcp_seconds":10,'
    '"max_mcp_retries":2,"max_reflections":1}'
)


async def _seed(admin, actor):
    task, attempt = uuid4(), uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO presales.resources VALUES(:id,'task',:parent)",
            id=task,
            parent=demo_id("opportunity-a"),
        )
        await execute(
            conn,
            """INSERT INTO presales.tasks(id,opportunity_id,goal,created_by,state,input_revision)
            VALUES(:task,:opportunity,'Synthetic',:actor,'analyzing',1)""",
            task=task,
            opportunity=demo_id("opportunity-a"),
            actor=actor,
        )
    return task, attempt


async def _version(admin, resource, actor):
    version = uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,
             input_manifest,created_by)
            VALUES(:id,:resource,1,'original','synthetic',false,false,'[]',:actor)""",
            id=version,
            resource=resource,
            actor=actor,
        )
        await execute(
            conn,
            "UPDATE presales.resource_versions SET ready=true,dependencies_complete=true WHERE id=:id",
            id=version,
        )
    return version


async def _attempt(admin, task, actor, *, generation=1):
    attempt = uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO presales.proposal_analysis_attempts
            (id,task_id,actor,thread_id,input_revision,execution_generation,execution_stage,
             start_scope,budget,model_mode,config_identity,run_revision,reflection_revision)
            VALUES(:id,:task,:actor,:thread,1,:generation,'running',CAST(:scope AS jsonb),
                   CAST(:budget AS jsonb),'fake','config',0,0)""",
            id=attempt,
            task=task,
            actor=actor,
            thread=f"synthetic-{attempt}",
            generation=generation,
            scope=f'["{task}"]',
            budget=BUDGET,
        )
    return attempt


def _source(version, *, kind="material"):
    return SourceReference(
        kind=kind,
        source_version_id=version,
        locator={"page": 1},
        content_sha256="a" * 64,
        declaration="synthetic",
        applicability={"products": ["synthetic-product"]},
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_runtime_cannot_rewrite_snapshot_content(services):
    service, _, admin, _ = services
    actor = demo_id("architect")
    task, attempt = await _seed(admin, actor)
    async with admin.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO presales.proposal_analysis_attempts
            (id,task_id,actor,thread_id,input_revision,execution_generation,execution_stage,
             start_scope,budget,model_mode,config_identity,run_revision,reflection_revision)
            VALUES(:id,:task,:actor,:thread,1,1,'running',CAST(:scope AS jsonb),
                   CAST(:budget AS jsonb),'fake','config',0,0)""",
            id=attempt,
            task=task,
            actor=actor,
            thread=f"synthetic-{attempt}",
            scope=f'["{task}"]',
            budget=BUDGET,
        )
        await execute(
            conn,
            "INSERT INTO presales.proposal_task_slots VALUES(:task,:attempt,1,:actor,now()+interval '1 hour')",
            task=task,
            attempt=attempt,
            actor=actor,
        )
        await execute(
            conn,
            "INSERT INTO presales.proposal_global_slot VALUES(true,:attempt,1,:actor,now()+interval '1 hour')",
            attempt=attempt,
            actor=actor,
        )
    artifact = uuid4()
    async with service.database.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO presales.evidence_snapshots
            (artifact_id,attempt_id,node_name,input_revision,execution_generation,
             reflection_revision,technical_retry,state,output_digest,body)
            VALUES(:id,:attempt,'node',1,1,0,0,'ready',:digest,'{}')""",
            id=artifact,
            attempt=attempt,
            digest="b" * 64,
        )
    with pytest.raises(StorageUnavailable):
        async with service.database.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.evidence_snapshots SET body='{\"tampered\":true}' WHERE artifact_id=:id",
                id=artifact,
            )
    async with admin.transaction() as conn:
        stored = await rows(
            conn,
            "SELECT body FROM presales.evidence_snapshots WHERE artifact_id=:id",
            id=artifact,
        )
    assert stored[0]["body"] == {}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_snapshot_rows_are_immutable_and_sources_need_a_real_version(services):
    service, _, admin, _ = services
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                """INSERT INTO presales.evidence_snapshot_sources
                (artifact_id,ordinal,kind,source_version_id,locator,content_sha256,
                 declaration,applicability)
                VALUES(:id,0,'material',:source,'{}',:digest,'synthetic','{}')""",
                id=uuid4(),
                source=uuid4(),
                digest="c" * 64,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cycle_is_rejected_by_the_database_guard(services):
    service, _, admin, _ = services
    actor = demo_id("architect")
    task, _ = await _seed(admin, actor)
    attempt = await _attempt(admin, task, actor)
    first, second = uuid4(), uuid4()
    async with admin.transaction() as conn:
        for artifact in (first, second):
            await execute(
                conn,
                """INSERT INTO presales.evidence_snapshots
                (artifact_id,attempt_id,node_name,input_revision,execution_generation,
                 reflection_revision,technical_retry,state,output_digest,body)
                VALUES(:id,:attempt,:node,1,1,0,0,'ready',:digest,'{}')""",
                id=artifact,
                attempt=attempt,
                node=str(artifact),
                digest="d" * 64,
            )
        await execute(
            conn,
            "INSERT INTO presales.evidence_snapshot_upstream VALUES(:child,0,:parent)",
            child=second,
            parent=first,
        )
    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                "INSERT INTO presales.evidence_snapshot_upstream VALUES(:child,0,:parent)",
                child=first,
                parent=second,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_persists_closure_and_reuses_identity(services):
    service, _, admin, _ = services
    actor = demo_id("architect")
    task, attempt = await _seed(admin, actor)
    version = await _version(admin, task, actor)
    async with admin.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO presales.proposal_analysis_attempts
            (id,task_id,actor,thread_id,input_revision,execution_generation,execution_stage,
             start_scope,budget,model_mode,config_identity,run_revision,reflection_revision)
            VALUES(:id,:task,:actor,:thread,1,1,'running',CAST(:scope AS jsonb),
                   CAST(:budget AS jsonb),'fake','config',0,0)""",
            id=attempt,
            task=task,
            actor=actor,
            thread=f"synthetic-{attempt}",
            scope=f'["{task}"]',
            budget=BUDGET,
        )
        await execute(
            conn,
            "INSERT INTO presales.proposal_task_slots VALUES(:task,:attempt,1,:actor,now()+interval '1 hour')",
            task=task,
            attempt=attempt,
            actor=actor,
        )
        await execute(
            conn,
            "INSERT INTO presales.proposal_global_slot VALUES(true,:attempt,1,:actor,now()+interval '1 hour')",
            attempt=attempt,
            actor=actor,
        )
    identity = AttemptIdentity(
        attempt_id=attempt,
        task_id=task,
        actor_id=actor,
        thread_id=f"synthetic-{attempt}",
        input_revision=1,
        execution_generation=1,
    )
    context = ProposalAttemptContext(service.database, identity)
    snapshots = EvidenceSnapshotService(context)
    node = NodeExecutionIdentity(
        attempt_id=attempt,
        node_name="requirements",
        input_revision=1,
        execution_generation=1,
    )
    first = await snapshots.save(node, body={"requirements": ["r1"]}, sources=(_source(version),))
    second = await snapshots.save(node, body={"requirements": ["r1"]}, sources=(_source(version),))
    assert second.artifact_id == first.artifact_id
    async with admin.transaction() as conn:
        count = await rows(
            conn,
            "SELECT count(*) AS total FROM presales.evidence_snapshots WHERE attempt_id=:id",
            id=attempt,
        )
        sources = await rows(
            conn,
            "SELECT kind,locator,content_sha256 FROM presales.evidence_snapshot_sources"
            " WHERE artifact_id=:id",
            id=first.artifact_id,
        )
    assert count[0]["total"] == 1
    assert sources[0]["kind"] == "material"
    assert sources[0]["locator"] == {"page": 1}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stale_generation_is_refused_and_nothing_is_written(services):
    service, _, admin, _ = services
    actor = demo_id("architect")
    task, _ = await _seed(admin, actor)
    attempt = await _attempt(admin, task, actor, generation=2)
    version = await _version(admin, task, actor)
    async with admin.transaction() as conn:
        await execute(
            conn,
            "UPDATE presales.proposal_task_slots SET attempt_id=:attempt,execution_generation=2,"
            " owner=:actor,lease_until=now()+interval '1 hour' WHERE task_id=:task",
            task=task,
            attempt=attempt,
            actor=actor,
        )
        await execute(
            conn,
            "UPDATE presales.proposal_global_slot SET attempt_id=:attempt,execution_generation=2,"
            " owner=:actor,lease_until=now()+interval '1 hour'",
            attempt=attempt,
            actor=actor,
        )
    identity = AttemptIdentity(
        attempt_id=attempt,
        task_id=task,
        actor_id=actor,
        thread_id=f"synthetic-{attempt}",
        input_revision=1,
        execution_generation=1,
    )
    from presales.domain.proposal_analysis_attempt import FenceConflict

    with pytest.raises(FenceConflict):
        await EvidenceSnapshotService(ProposalAttemptContext(service.database, identity)).save(
            NodeExecutionIdentity(
                attempt_id=attempt,
                node_name="stale",
                input_revision=1,
                execution_generation=1,
            ),
            body={},
            sources=(_source(version),),
        )
    async with admin.transaction() as conn:
        stored = await rows(
            conn,
            "SELECT count(*) AS total FROM presales.evidence_snapshots WHERE attempt_id=:id",
            id=attempt,
        )
    assert stored[0]["total"] == 0
