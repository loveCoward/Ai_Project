import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from presales.domain.contracts import Actor, StorageUnavailable, digest
from presales.domain.proposal_analysis_attempt import (
    AttemptStage,
    AttemptTerminal,
    FenceConflict,
    GlobalBusy,
    ModelMode,
    ProposalAnalysisAttempt,
)
from presales.infrastructure.database import execute, rows
from presales.infrastructure.proposal_attempt_store import ProposalAttemptStore
from presales.infrastructure.proposal_checkpoint import (
    ProposalCheckpointFence,
    wrap_checkpoint_saver,
)
from presales.infrastructure.proposal_execution_records import (
    ProposalExecutionRecord,
    ProposalExecutionRecords,
)
from presales.maintenance.service import demo_id

pytestmark = pytest.mark.postgres
ARCH = Actor(subject=demo_id("architect"))
OPP = demo_id("opportunity-a")


def _dsn(sqlalchemy_url: str) -> str:
    return (
        make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


async def prepare_checkpoint_tables(admin) -> None:
    async with AsyncPostgresSaver.from_conn_string(_dsn(os.environ["PRESALES_TEST_URL"])) as saver:
        await saver.setup()
    async with admin.transaction() as conn:
        await execute(conn, "GRANT USAGE ON SCHEMA public TO presales_runtime")
        await execute(
            conn,
            """GRANT SELECT,INSERT,UPDATE,DELETE ON
            public.checkpoints,public.checkpoint_blobs,public.checkpoint_writes
            TO presales_runtime""",
        )


async def create_attempt(service, task_id: UUID, *, thread_id: str | None = None):
    async with service.database.transaction() as conn:
        source = (
            await rows(
                conn,
                "SELECT id FROM presales.resource_versions WHERE resource_id=:id",
                id=task_id,
            )
        )[0]["id"]
    attempt = ProposalAnalysisAttempt(
        id=uuid4(),
        task_id=task_id,
        actor_id=ARCH.subject,
        thread_id=thread_id or f"thread-{uuid4()}",
        input_revision=1,
        start_scope=(source,),
        model_mode=ModelMode.FAKE,
        config_identity="fake-config",
    )
    async with service.database.transaction() as conn:
        store = ProposalAttemptStore(conn)
        await store.create_attempt(
            attempt,
            owner=ARCH.subject,
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        await store.transition(
            attempt.id,
            from_stage=AttemptStage.ACCEPTED,
            stage=AttemptStage.RUNNING,
            outcome=None,
            expected_generation=1,
            expected_run_revision=0,
        )
    return attempt


def test_migration_upgrade_head_twice(test_url):
    engine = create_engine(test_url, hide_parameters=True)
    with engine.begin() as conn:
        cfg = Config(str(Path("alembic.ini").resolve()))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT version_num FROM presales_migration_version")
        ).scalar_one()
        assert version == "0011_evidence_snapshot"
    engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_attempt_rejects_checkpoint_and_pending_writes(services):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic cancelled checkpoint", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    await prepare_checkpoint_tables(admin)
    fence = ProposalCheckpointFence(
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        thread_id=attempt.thread_id,
        execution_generation=1,
    )
    async with AsyncPostgresSaver.from_conn_string(
        _dsn(os.environ["PRESALES_TEST_RUNTIME_URL"])
    ) as base:
        saver = wrap_checkpoint_saver(base, fence)
        config = {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}}
        checkpoint = {
            "v": 4,
            "id": "cp-cancelled",
            "ts": datetime.now(UTC).isoformat(),
            "channel_values": {},
            "channel_versions": {},
            "versions_seen": {},
            "updated_channels": None,
        }
        written = await saver.aput(config, checkpoint, {}, {})
        async with admin.transaction() as conn:
            await execute(
                conn,
                """UPDATE presales.proposal_analysis_attempts
                SET cancelled_at=now(),execution_stage='reconciling',run_revision=run_revision+1
                WHERE id=:id""",
                id=attempt.id,
            )
        with pytest.raises(AttemptTerminal):
            await saver.aput(
                {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}},
                {**checkpoint, "id": "cp-cancelled-late"},
                {},
                {},
            )
        with pytest.raises(AttemptTerminal):
            await saver.aput_writes(written, [("messages", "late")], "task-cancelled-late")


@pytest.mark.asyncio
async def test_task_and_global_slots_are_exclusive(services):
    service, _, _, _ = services
    task_a = await service.create_task(ARCH, OPP, "Synthetic A", uuid4())
    task_b = await service.create_task(ARCH, OPP, "Synthetic B", uuid4())
    attempt_a = await create_attempt(service, UUID(task_a["resource_id"]))
    async with service.database.transaction() as conn:
        store = ProposalAttemptStore(conn)
        assert (await store.task_slot(attempt_a.task_id))["attempt_id"] == attempt_a.id
        assert (await store.global_slot())["attempt_id"] == attempt_a.id

    busy = ProposalAnalysisAttempt(
        id=uuid4(),
        task_id=UUID(task_b["resource_id"]),
        actor_id=ARCH.subject,
        thread_id=f"thread-{uuid4()}",
        input_revision=1,
        start_scope=(uuid4(),),
        model_mode=ModelMode.FAKE,
        config_identity="fake-config",
    )
    with pytest.raises(GlobalBusy):
        async with service.database.transaction() as conn:
            await ProposalAttemptStore(conn).create_attempt(
                busy,
                owner=ARCH.subject,
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
    async with service.database.transaction() as conn:
        await ProposalAttemptStore(conn).release_slots(attempt_a.id, 1)
    async with service.database.transaction() as conn:
        await ProposalAttemptStore(conn).create_attempt(
            busy,
            owner=ARCH.subject,
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_runtime_and_maintenance_roles_cannot_write_identity(services):
    service, maintenance, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic roles", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))

    with pytest.raises(StorageUnavailable):
        async with service.database.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.proposal_analysis_attempts SET actor=:actor WHERE id=:id",
                actor=uuid4(),
                id=attempt.id,
            )

    with pytest.raises(StorageUnavailable):
        async with maintenance.database.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.proposal_analysis_attempts SET error_code='maintenance' WHERE id=:id",
                id=attempt.id,
            )


@pytest.mark.asyncio
async def test_checkpoint_and_pending_writes_are_fenced_by_generation(services):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic checkpoint", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    runtime_dsn = _dsn(os.environ["PRESALES_TEST_RUNTIME_URL"])
    await prepare_checkpoint_tables(admin)

    fence = ProposalCheckpointFence(
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        thread_id=attempt.thread_id,
        execution_generation=1,
    )
    async with AsyncPostgresSaver.from_conn_string(runtime_dsn) as base:
        saver = wrap_checkpoint_saver(base, fence)
        config = {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}}
        checkpoint = {
            "v": 4,
            "id": "cp-1",
            "ts": datetime.now(UTC).isoformat(),
            "channel_values": {},
            "channel_versions": {},
            "versions_seen": {},
            "updated_channels": None,
        }
        written = await saver.aput(config, checkpoint, {}, {})
        await saver.aput_writes(written, [("messages", "synthetic")], "task-1")

        async with admin.transaction() as conn:
            await execute(
                conn,
                """UPDATE presales.proposal_analysis_attempts
                SET execution_generation=execution_generation+1,run_revision=run_revision+1
                WHERE id=:id""",
                id=attempt.id,
            )

        with pytest.raises(FenceConflict):
            await saver.aput(
                {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}},
                {**checkpoint, "id": "cp-late"},
                {},
                {},
            )
        with pytest.raises(FenceConflict):
            await saver.aput_writes(written, [("messages", "late")], "task-late")
    engine = create_engine(os.environ["PRESALES_TEST_URL"])
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM checkpoint_writes WHERE task_id='task-late'")
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM checkpoints WHERE checkpoint_id='cp-late'")
            ).scalar_one()
            == 0
        )
    engine.dispose()


def checkpoint_payload(identity):
    return {
        "v": 4,
        "id": identity,
        "ts": datetime.now(UTC).isoformat(),
        "channel_values": {"messages": ["synthetic"]},
        "channel_versions": {"messages": "1"},
        "versions_seen": {},
        "updated_channels": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["checkpoint", "pending_writes"])
@pytest.mark.parametrize("invalidate", ["takeover", "cancel"])
async def test_paused_owner_cannot_commit_after_invalidation(
    services, monkeypatch, operation, invalidate
):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic paused owner", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    await prepare_checkpoint_tables(admin)
    fence = ProposalCheckpointFence(attempt.id, attempt.task_id, attempt.thread_id, 1)
    paused, resume = asyncio.Event(), asyncio.Event()
    original = ProposalCheckpointFence.check

    async def paused_check(self, conn):
        paused.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return await original(self, conn)

    config = {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}}
    async with AsyncPostgresSaver.from_conn_string(
        _dsn(os.environ["PRESALES_TEST_RUNTIME_URL"])
    ) as base:
        saver = wrap_checkpoint_saver(base, fence)
        config = await saver.aput(config, checkpoint_payload("paused-base"), {}, {"messages": "1"})
        monkeypatch.setattr(ProposalCheckpointFence, "check", paused_check)
        if operation == "checkpoint":
            pending = asyncio.create_task(
                saver.aput(config, checkpoint_payload("paused-late"), {}, {"messages": "2"})
            )
        else:
            pending = asyncio.create_task(
                saver.aput_writes(config, [("messages", "late")], "paused-late")
            )
        try:
            await asyncio.wait_for(paused.wait(), timeout=10)
            async with admin.transaction() as conn:
                if invalidate == "takeover":
                    await execute(
                        conn,
                        """UPDATE presales.proposal_global_slot SET execution_generation=2
                        WHERE attempt_id=:id""",
                        id=attempt.id,
                    )
                    await execute(
                        conn,
                        """UPDATE presales.proposal_task_slots SET execution_generation=2
                        WHERE attempt_id=:id""",
                        id=attempt.id,
                    )
                    await execute(
                        conn,
                        """UPDATE presales.proposal_analysis_attempts
                        SET execution_generation=2,run_revision=run_revision+1 WHERE id=:id""",
                        id=attempt.id,
                    )
                else:
                    await ProposalAttemptStore(conn).mark_cancelled(
                        attempt.id, expected_generation=1, expected_run_revision=1
                    )
            resume.set()
            with pytest.raises(FenceConflict if invalidate == "takeover" else AttemptTerminal):
                await asyncio.wait_for(pending, timeout=10)
            assert saver.last_receipt is None
        finally:
            resume.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    async with admin.transaction() as conn:
        for table, condition in [
            ("public.checkpoints", "checkpoint_id='paused-late'"),
            ("public.checkpoint_writes", "task_id='paused-late'"),
            ("public.checkpoint_blobs", "version='2'"),
            (
                "presales.proposal_checkpoint_receipts",
                "checkpoint_id='paused-late' OR write_task_id='paused-late'",
            ),
        ]:
            count = await rows(
                conn,
                f"SELECT count(*) AS n FROM {table} WHERE thread_id=:thread AND ({condition})"
                if table.startswith("public.")
                else f"SELECT count(*) AS n FROM {table} WHERE attempt_id=:attempt AND ({condition})",
                thread=attempt.thread_id,
                attempt=attempt.id,
            )
            assert count[0]["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_table", ["public.checkpoints", "presales.proposal_checkpoint_receipts"]
)
async def test_checkpoint_failure_rolls_back_blobs_and_receipt(services, failure_table):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic rollback", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    await prepare_checkpoint_tables(admin)
    async with admin.transaction() as conn:
        await execute(
            conn,
            """CREATE FUNCTION public.fail_synthetic_checkpoint() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic_commit_failure'; END $$""",
        )
        await execute(
            conn,
            f"""CREATE TRIGGER synthetic_failure BEFORE INSERT ON {failure_table}
        FOR EACH ROW EXECUTE FUNCTION public.fail_synthetic_checkpoint()""",
        )
    try:
        async with AsyncPostgresSaver.from_conn_string(
            _dsn(os.environ["PRESALES_TEST_RUNTIME_URL"])
        ) as base:
            saver = wrap_checkpoint_saver(
                base, ProposalCheckpointFence(attempt.id, attempt.task_id, attempt.thread_id, 1)
            )
            config = {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}}
            import psycopg

            with pytest.raises(psycopg.errors.RaiseException, match="synthetic_commit_failure"):
                await saver.aput(config, checkpoint_payload("rollback"), {}, {"messages": "1"})
            assert saver.last_receipt is None
        async with admin.transaction() as conn:
            assert (
                await rows(
                    conn,
                    "SELECT count(*) AS n FROM public.checkpoint_blobs WHERE thread_id=:id",
                    id=attempt.thread_id,
                )
            )[0]["n"] == 0
            assert (
                await rows(
                    conn,
                    "SELECT count(*) AS n FROM presales.proposal_checkpoint_receipts WHERE attempt_id=:id",
                    id=attempt.id,
                )
            )[0]["n"] == 0
    finally:
        async with admin.transaction() as conn:
            await execute(conn, f"DROP TRIGGER synthetic_failure ON {failure_table}")
            await execute(conn, "DROP FUNCTION public.fail_synthetic_checkpoint()")


@pytest.mark.asyncio
async def test_checkpoint_guard_serializes_cancel_until_commit(services, monkeypatch):
    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic concurrent cancel", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    await prepare_checkpoint_tables(admin)
    checked, release, cancellation_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ProposalCheckpointFence.check

    async def holding_check(self, conn):
        receipt = await original(self, conn)
        checked.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return receipt

    monkeypatch.setattr(ProposalCheckpointFence, "check", holding_check)
    cancellation_pid = None

    async def cancel():
        nonlocal cancellation_pid
        async with admin.transaction() as conn:
            cancellation_pid = (await rows(conn, "SELECT pg_backend_pid() AS pid"))[0]["pid"]
            cancellation_started.set()
            await ProposalAttemptStore(conn).mark_cancelled(
                attempt.id, expected_generation=1, expected_run_revision=1
            )

    async with AsyncPostgresSaver.from_conn_string(
        _dsn(os.environ["PRESALES_TEST_RUNTIME_URL"])
    ) as base:
        saver = wrap_checkpoint_saver(
            base, ProposalCheckpointFence(attempt.id, attempt.task_id, attempt.thread_id, 1)
        )
        config = {"configurable": {"thread_id": attempt.thread_id, "checkpoint_ns": ""}}
        writing = asyncio.create_task(
            saver.aput(config, checkpoint_payload("serial"), {}, {"messages": "1"})
        )
        cancelling = None
        try:
            await asyncio.wait_for(checked.wait(), timeout=10)
            assert saver.last_receipt is None
            cancelling = asyncio.create_task(cancel())
            await asyncio.wait_for(cancellation_started.wait(), timeout=10)
            async with asyncio.timeout(10):
                while True:
                    async with admin.transaction() as conn:
                        activity = await rows(
                            conn,
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid",
                            pid=cancellation_pid,
                        )
                    if activity and activity[0]["wait_event_type"] == "Lock":
                        break
                    await asyncio.sleep(0.01)
            assert not cancelling.done()
            release.set()
            await asyncio.wait_for(writing, timeout=10)
            assert saver.last_receipt is not None
            await asyncio.wait_for(cancelling, timeout=10)
        finally:
            release.set()
            pending = [job for job in (writing, cancelling) if job is not None]
            for job in pending:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    async with admin.transaction() as conn:
        receipts = await rows(
            conn,
            "SELECT * FROM presales.proposal_checkpoint_receipts WHERE attempt_id=:id",
            id=attempt.id,
        )
        assert len(receipts) == 1
        assert receipts[0]["execution_generation"] == 1
        assert receipts[0]["run_revision"] == 1
        assert receipts[0]["checkpoint_id"] == "serial"


@pytest.mark.asyncio
async def test_execution_records_are_idempotent_immutable_and_generation_fenced(services):
    from presales.domain.contracts import Conflict

    service, _, admin, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic execution records", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    for kind in ("call", "recovery", "node_result"):
        record = ProposalExecutionRecord(
            attempt_id=attempt.id,
            kind=kind,
            record_key="synthetic",
            execution_generation=1,
            input_digest="a" * 64,
            status="registered",
            body={"mode": "fake"},
        )
        async with service.database.transaction() as conn:
            records = ProposalExecutionRecords(conn)
            assert await records.save(record) == record
            assert await records.save(record) == record
        with pytest.raises(Conflict):
            async with service.database.transaction() as conn:
                await ProposalExecutionRecords(conn).save(
                    record.model_copy(update={"input_digest": "b" * 64})
                )
        async with service.database.transaction() as conn:
            completed = await ProposalExecutionRecords(conn).complete(
                record, status="completed", body={"outcome": "synthetic"}
            )
            assert completed.status == "completed"
        with pytest.raises(Conflict):
            async with service.database.transaction() as conn:
                await ProposalExecutionRecords(conn).complete(record, status="failed", body={})
    async with admin.transaction() as conn:
        await execute(
            conn,
            """UPDATE presales.proposal_analysis_attempts SET
        execution_generation=2,run_revision=run_revision+1 WHERE id=:id""",
            id=attempt.id,
        )
    with pytest.raises(FenceConflict):
        async with service.database.transaction() as conn:
            await ProposalExecutionRecords(conn).save(
                record.model_copy(update={"record_key": "late"})
            )


async def _park_reconciling(
    service, attempt_id: UUID, *, with_intent: bool, request: UUID | None = None
):
    async with service.database.transaction() as conn:
        await execute(
            conn,
            """UPDATE presales.proposal_analysis_attempts
            SET execution_stage='reconciling',run_revision=run_revision+1,
                error_code='restart_unconfirmed' WHERE id=:id""",
            id=attempt_id,
        )
        if with_intent:
            body = {
                "type": "resume_intent",
                "request_id": str(request),
                "checkpoint_id": "cp-resume",
                "input_digest": "0" * 64,
                "expected_run_revision": 2,
                "next_execution_generation": 2,
            }
            await ProposalExecutionRecords(conn).save(
                ProposalExecutionRecord(
                    attempt_id=attempt_id,
                    kind="recovery",
                    record_key=f"recovery-intent-{request}",
                    execution_generation=1,
                    input_digest=digest(body),
                    status="completed",
                    body=body,
                )
            )


@pytest.mark.asyncio
async def test_resume_function_requires_a_recorded_intent(services):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic resume without intent", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    await _park_reconciling(service, attempt.id, with_intent=False)
    async with service.database.transaction() as conn:
        with pytest.raises(Exception) as error:
            await ProposalAttemptStore(conn).resume_attempt(
                attempt.id,
                actor=ARCH.subject,
                expected_generation=1,
                expected_run_revision=2,
                expected_task_revision=1,
                expected_input_revision=1,
                intent_request=uuid4(),
            )
    assert "recovery_blocked" in str(error.value)
    async with service.database.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt.id,
            )
        )[0]
    assert (row["execution_stage"], row["execution_generation"]) == ("reconciling", 1)


@pytest.mark.asyncio
async def test_resume_function_bumps_generation_and_reacquires_slots(services):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic resume with intent", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    request = uuid4()
    await _park_reconciling(service, attempt.id, with_intent=True, request=request)
    async with service.database.transaction() as conn:
        store = ProposalAttemptStore(conn)
        resumed = await store.resume_attempt(
            attempt.id,
            actor=ARCH.subject,
            expected_generation=1,
            expected_run_revision=2,
            expected_task_revision=1,
            expected_input_revision=1,
            intent_request=request,
        )
    assert resumed == 2
    async with service.database.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt.id,
            )
        )[0]
        slot = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_task_slots WHERE task_id=:t",
                t=attempt.task_id,
            )
        )[0]
        global_slot = (
            await rows(conn, "SELECT * FROM presales.proposal_global_slot WHERE singleton=true")
        )[0]
    assert (row["execution_stage"], row["execution_generation"]) == ("running", 2)
    assert slot["execution_generation"] == 2
    assert global_slot["execution_generation"] == 2


@pytest.mark.asyncio
async def test_trigger_rejects_reconciling_to_running_without_intent(services):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic trigger guard", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    await _park_reconciling(service, attempt.id, with_intent=False)
    async with service.database.transaction() as conn:
        with pytest.raises(Exception) as error:
            await execute(
                conn,
                """UPDATE presales.proposal_analysis_attempts
                SET execution_stage='running',execution_generation=2,
                    run_revision=run_revision+1 WHERE id=:id""",
                id=attempt.id,
            )
    assert "invalid_proposal_attempt_transition" in str(error.value)
    async with service.database.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt.id,
            )
        )[0]
    assert (row["execution_stage"], row["execution_generation"]) == ("reconciling", 1)


@pytest.mark.asyncio
async def test_trigger_rejects_reconciling_to_running_on_same_generation(services):
    service, _, _, _ = services
    task = await service.create_task(ARCH, OPP, "Synthetic same-generation", uuid4())
    attempt = await create_attempt(service, UUID(task["resource_id"]))
    request = uuid4()
    await _park_reconciling(service, attempt.id, with_intent=True, request=request)
    async with service.database.transaction() as conn:
        with pytest.raises(Exception) as error:
            await execute(
                conn,
                """UPDATE presales.proposal_analysis_attempts
                SET execution_stage='running',run_revision=run_revision+1 WHERE id=:id""",
                id=attempt.id,
            )
    assert "invalid_proposal_attempt_transition" in str(error.value)
    async with service.database.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt.id,
            )
        )[0]
    assert (row["execution_stage"], row["execution_generation"]) == ("reconciling", 1)
