"""Real PostgreSQL lifecycle races: real processes, real kills, real connection loss."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from presales.application.proposal_attempt_commands import (
    CancelAttemptCommand,
    ProposalAttemptCancel,
    ProposalAttemptStart,
)
from presales.application.proposal_attempt_recovery import ProposalAttemptRecovery
from presales.application.proposal_attempt_runtime import (
    ProposalAttemptSupervisor,
    SupervisorSettings,
)
from presales.domain.contracts import Actor, StorageUnavailable, digest
from presales.domain.proposal_analysis_attempt import (
    BackgroundRequirement,
    ModelMode,
    RecoveryIntent,
    StartAttemptCommand,
)
from presales.infrastructure.config import OutboundServiceSettings
from presales.infrastructure.database import execute, rows
from presales.infrastructure.proposal_attempt_store import ProposalAttemptStore
from presales.infrastructure.proposal_model import ProposalModel
from presales.maintenance.service import demo_id

pytestmark = pytest.mark.postgres

ARCH = Actor(subject=demo_id("architect"))
OPP = demo_id("opportunity-a")
CONFIG = "fake-config"


WORKER = textwrap.dedent(
    '''
    import asyncio, json, os, selectors, sys

    def run(coro):
        asyncio.run(coro, loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    from uuid import UUID

    from presales.application.proposal_attempt_commands import ProposalAttemptStart
    from presales.domain.contracts import Actor, DomainError
    from presales.domain.proposal_analysis_attempt import (
        BackgroundRequirement, ModelMode, StartAttemptCommand,
    )
    from presales.infrastructure.config import (
        DatabaseSettings, OutboundServiceSettings,
    )
    from presales.infrastructure.database import Database
    from presales.infrastructure.proposal_model import ProposalModel


    def model():
        return ProposalModel(
            OutboundServiceSettings(
                mode="fake", allowed_data_classes=frozenset({"synthetic", "owned"})
            ),
            "fake-config",
            fake=lambda prompt: "synthetic",
        )


    async def start(argv):
        task, request, revision, input_revision, actor, source = argv
        try:
            settings = DatabaseSettings(
                database_url=os.environ["PRESALES_TEST_RUNTIME_URL"],
                file_root=os.environ["PRESALES_TEST_FILE_ROOT"],
            )
            database = Database(settings)
        except Exception as error:
            print(json.dumps({"ok": False, "code": "settings:" + repr(error)}), flush=True)
            raise SystemExit(1)
        command = StartAttemptCommand(
            request_id=UUID(request),
            task_id=UUID(task),
            expected_task_revision=int(revision),
            expected_input_revision=int(input_revision),
            model_mode=ModelMode.FAKE,
            config_identity="fake-config",
            start_scope=(UUID(source),),
            background_requirement=BackgroundRequirement.OPTIONAL,
        )
        try:
            result = await ProposalAttemptStart(
                database, model=model()
            ).start(Actor(subject=UUID(actor)), command)
            print(json.dumps({"ok": True, "attempt": result["attempt_id"]}), flush=True)
        except DomainError as error:
            print(json.dumps({"ok": False, "code": error.code}), flush=True)
        except Exception as error:
            print(json.dumps({"ok": False, "code": repr(error)}), flush=True)
            print(json.dumps({"ok": False, "code": type(error).__name__}), flush=True)
        finally:
            await database.close()


    async def hold(argv):
        """Reads the live generation, waits for a go signal, then writes with the old one."""
        (attempt,) = argv
        import psycopg

        with psycopg.connect(os.environ["PRESALES_TEST_RUNTIME_DSN"]) as conn:
            row = conn.execute(
                "SELECT execution_generation FROM presales.proposal_analysis_attempts"
                " WHERE id=%s", (attempt,)
            ).fetchone()
            generation = row[0]
            print(json.dumps({"ready": True, "generation": generation}), flush=True)
            sys.stdin.readline()
            try:
                conn.execute(
                    "UPDATE presales.proposal_analysis_attempts"
                    " SET execution_generation=execution_generation,run_revision=run_revision+1"
                    " WHERE id=%s AND execution_generation=%s",
                    (attempt, generation),
                )
                conn.execute(
                    "INSERT INTO presales.proposal_execution_records"
                    "(attempt_id,kind,record_key,execution_generation,input_digest,status,body)"
                    " VALUES(%s,'recovery','old-owner-write',%s,%s,'completed','{}'::jsonb)",
                    (attempt, generation, "0" * 64),
                )
                conn.commit()
                print(json.dumps({"ok": True}), flush=True)
            except Exception as error:
                conn.rollback()
                print(json.dumps({"ok": False, "code": str(error)}), flush=True)


    if sys.argv[1] == "start":
        run(start(sys.argv[2:]))
    else:
        run(hold(sys.argv[2:]))
    '''
)


@pytest.fixture(scope="module")
def worker_script(tmp_path_factory):
    path = Path(tmp_path_factory.mktemp("worker")) / "lifecycle_worker.py"
    path.write_text(WORKER, encoding="utf-8")
    return path


def _dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://")


def _env() -> dict:
    environment = dict(os.environ)
    environment["PRESALES_TEST_RUNTIME_DSN"] = _dsn(os.environ["PRESALES_TEST_RUNTIME_URL"])
    environment["PRESALES_DATABASE_URL"] = os.environ["PRESALES_TEST_RUNTIME_URL"]
    environment["PRESALES_TEST_FILE_ROOT"] = str(
        Path(os.environ.get("PRESALES_TEST_FILE_ROOT", ".")).resolve()
    )
    root = Path(__file__).resolve().parents[2]
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return environment


def _spawn(worker_script: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(worker_script), *args],
        cwd=Path(__file__).resolve().parents[2],
        env=_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


async def _seed_startable_task(service) -> dict:
    async with service.database.transaction() as conn:
        if not await rows(conn, "SELECT 1 FROM presales.proposal_global_slot"):
            await execute(conn, "INSERT INTO presales.proposal_global_slot(singleton) VALUES(true)")
    task = await service.create_task(ARCH, OPP, "Synthetic lifecycle task", uuid4())
    task_id = UUID(task["resource_id"])
    material, version, result_version, job = uuid4(), uuid4(), uuid4(), uuid4()
    async with service.database.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO presales.resources VALUES(:id,'material',:task)",
            id=material,
            task=task_id,
        )
        await execute(
            conn,
            """INSERT INTO presales.materials(id,opportunity_id,task_id,filename)
            VALUES(:id,NULL,:task,'synthetic.md')""",
            id=material,
            task=task_id,
        )
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,
             input_manifest,created_by)
            VALUES(:id,:resource,1,'original','synthetic',true,true,'[]',:actor)""",
            id=version,
            resource=material,
            actor=ARCH.subject,
        )
        await execute(
            conn,
            """INSERT INTO presales.material_versions
            (id,material_id,storage_key,sha256,size_bytes)
            VALUES(:id,:material,:key,:sha,4)""",
            id=version,
            material=material,
            key=f"{uuid4().hex}.bin",
            sha="a" * 64,
        )
        await execute(
            conn,
            "INSERT INTO presales.file_readiness(id,state) VALUES(:id,'ready')",
            id=version,
        )
        await execute(
            conn,
            "INSERT INTO presales.task_inputs VALUES(:task,:version)",
            task=task_id,
            version=version,
        )
        await execute(
            conn,
            """INSERT INTO presales.task_input_manifest
            (task_id,input_revision,material_version_id,necessity,marked_by)
            VALUES(:task,1,:version,'required',:actor)""",
            task=task_id,
            version=version,
            actor=ARCH.subject,
        )
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,
             input_manifest,created_by)
            VALUES(:id,:material,2,'original','synthetic',true,true,'[]',:actor)""",
            id=result_version,
            material=material,
            actor=ARCH.subject,
        )
        await execute(
            conn,
            """INSERT INTO presales.parse_jobs
            (id,source_version_id,sha256,parser_version,state,lease_until,created_by)
            VALUES(:job,:source,:sha,'located-1.0','succeeded',now()+interval '1 hour',:actor)""",
            job=job,
            source=version,
            sha="b" * 64,
            actor=ARCH.subject,
        )
        await execute(
            conn,
            """INSERT INTO presales.parse_results
            (id,job_id,source_version_id,document,character_count)
            VALUES(:id,:job,:source,CAST(:document AS jsonb),4)""",
            id=result_version,
            job=job,
            source=version,
            document=json.dumps(
                {"parser_version": "located-1.0", "units": [], "character_count": 4}
            ),
        )
    return {"task_id": task_id, "source": version, **task}


def _command(task: dict, request: UUID | None = None) -> StartAttemptCommand:
    return StartAttemptCommand(
        request_id=request or uuid4(),
        task_id=task["task_id"],
        expected_task_revision=task["revision"],
        expected_input_revision=1,
        model_mode=ModelMode.FAKE,
        config_identity=CONFIG,
        start_scope=(task["source"],),
        background_requirement=BackgroundRequirement.OPTIONAL,
    )


def _start(service, task: dict, request: UUID | None = None):
    model = ProposalModel(
        OutboundServiceSettings(
            mode="fake", allowed_data_classes=frozenset({"synthetic", "owned"})
        ),
        CONFIG,
        fake=lambda prompt: "synthetic",
    )
    return ProposalAttemptStart(service.database, model=model).start(ARCH, _command(task, request))


async def _attempt_rows(service, task_id: UUID) -> list[dict]:
    async with service.database.transaction() as conn:
        return await rows(
            conn,
            "SELECT * FROM presales.proposal_analysis_attempts WHERE task_id=:t",
            t=task_id,
        )


@pytest.mark.asyncio
async def test_two_real_processes_start_only_one_attempt(services, worker_script):
    service, _, _, _ = services
    task = await _seed_startable_task(service)
    children = [
        _spawn(
            worker_script,
            "start",
            str(task["task_id"]),
            str(uuid4()),
            str(task["revision"]),
            "1",
            str(ARCH.subject),
            str(task["source"]),
        )
        for _ in range(2)
    ]
    captured = [child.communicate(timeout=90) for child in children]
    outputs = [out.strip() for out, _ in captured]
    results = [json.loads(line) for out in outputs for line in out.splitlines() if line.strip()]
    assert sorted(item["ok"] for item in results) == [False, True], captured
    assert results[1 - [item["ok"] for item in results].index(True)]["code"] in {
        "attempt_busy",
        "global_busy",
        "revision_or_request_conflict",
    }
    attempts = await _attempt_rows(service, task["task_id"])
    assert len(attempts) == 1
    async with service.database.transaction() as conn:
        assert len(await rows(conn, "SELECT * FROM presales.proposal_task_slots")) == 1


@pytest.mark.asyncio
async def test_hard_killed_start_is_only_reconciled_never_dispatched(services, worker_script):
    service, _, _, _ = services
    task = await _seed_startable_task(service)
    child = _spawn(
        worker_script,
        "start",
        str(task["task_id"]),
        str(uuid4()),
        str(task["revision"]),
        "1",
        str(ARCH.subject),
        str(task["source"]),
    )
    assert child.communicate(timeout=90)[0]
    child.kill()
    child.wait(timeout=30)
    attempts = await _attempt_rows(service, task["task_id"])
    assert len(attempts) == 1
    assert attempts[0]["execution_stage"] == "accepted"
    supervisor = ProposalAttemptSupervisor(service.database, settings=SupervisorSettings())
    findings = await supervisor.scan()
    codes = {(finding.attempt_id, finding.code) for finding in findings}
    assert (UUID(str(attempts[0]["id"])), "accepted_not_dispatched") in codes
    assert (await _attempt_rows(service, task["task_id"]))[0]["execution_stage"] == "accepted"


async def _record_intent(admin, attempt_id: UUID, checkpoint: str = "cp-live") -> UUID:
    """Adds a completed resume intent plus its checkpoint receipt."""
    request = uuid4()
    async with admin.transaction() as conn:
        attempt = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt_id,
            )
        )[0]
        await execute(
            conn,
            """INSERT INTO presales.proposal_checkpoint_receipts
            (attempt_id,execution_generation,run_revision,operation,checkpoint_id,
             write_task_id,checked_at)
            VALUES(:id,:generation,:revision,'checkpoint',:checkpoint,'',now())""",
            id=attempt_id,
            generation=attempt["execution_generation"],
            revision=attempt["run_revision"],
            checkpoint=checkpoint,
        )
        body = {
            "type": "resume_intent",
            "request_id": str(request),
            "checkpoint_id": checkpoint,
            "input_digest": "f" * 64,
            "expected_run_revision": attempt["run_revision"],
            "next_execution_generation": attempt["execution_generation"] + 1,
        }
        await execute(
            conn,
            """INSERT INTO presales.proposal_execution_records
            (attempt_id,kind,record_key,execution_generation,input_digest,status,body)
            VALUES(:attempt,'recovery',:key,:generation,:digest,'completed',
                   CAST(:body AS jsonb))""",
            attempt=attempt_id,
            key=f"recovery-intent-{request}",
            generation=attempt["execution_generation"],
            digest=digest(body),
            body=json.dumps(body),
        )
    return request


async def _task_revision(service, task_id: UUID) -> int:
    async with service.database.transaction() as conn:
        return (await rows(conn, "SELECT revision FROM presales.tasks WHERE id=:t", t=task_id))[0][
            "revision"
        ]


async def _attempt_run_revision(service, attempt_id: UUID) -> int:
    async with service.database.transaction() as conn:
        rows_ = await rows(
            conn,
            "SELECT run_revision FROM presales.proposal_analysis_attempts WHERE id=:id",
            id=attempt_id,
        )
        return rows_[0]["run_revision"]


@pytest.mark.asyncio
async def test_old_owner_write_is_rejected_after_the_generation_advances(services, worker_script):
    service, _, admin, _ = services
    task = await _seed_startable_task(service)
    started = await _start(service, task)
    attempt_id = UUID(started["attempt_id"])
    async with admin.transaction() as conn:
        await execute(
            conn,
            """UPDATE presales.proposal_analysis_attempts
            SET execution_stage='reconciling',run_revision=run_revision+1
            WHERE id=:id""",
            id=attempt_id,
        )
    child = _spawn(worker_script, "hold", str(attempt_id))
    assert json.loads(child.stdout.readline())["generation"] == 1
    await _record_intent(admin, attempt_id)
    supervisor = ProposalAttemptSupervisor(service.database, settings=SupervisorSettings())
    claim = await supervisor.claim(attempt_id)
    assert claim.identity.execution_generation == 2
    child.stdin.write("go\n")
    child.stdin.flush()
    outcome = json.loads(child.stdout.readline())
    child.communicate(timeout=60)
    assert outcome["ok"] is False
    assert "invalid_proposal_execution_generation" in outcome["code"]


@pytest.mark.asyncio
async def test_cancel_keeps_global_slot_until_stop_is_confirmed(services):
    service, _, _, _ = services
    first = await _seed_startable_task(service)
    second = await _seed_startable_task(service)
    started = await _start(service, first)
    attempt_id = UUID(started["attempt_id"])
    current = await _task_revision(service, first["task_id"])
    await ProposalAttemptCancel(service.database).cancel(
        ARCH,
        CancelAttemptCommand(
            request_id=uuid4(),
            task_id=first["task_id"],
            expected_task_revision=current,
            attempt_id=attempt_id,
        ),
    )
    with pytest.raises(Exception) as blocked:
        await _start(service, second)
    assert "global_busy" in str(blocked.value)
    drain = await ProposalAttemptSupervisor(service.database).reconcile_cancelled(
        attempt_id, execution_stopped=False
    )
    assert drain.slots_released is False
    with pytest.raises(Exception) as still_blocked:
        await _start(service, second)
    assert "global_busy" in str(still_blocked.value)
    released = await ProposalAttemptSupervisor(service.database).reconcile_cancelled(
        attempt_id, execution_stopped=True
    )
    assert released.slots_released is True
    await _start(service, second)


@pytest.mark.asyncio
async def test_resume_keeps_budget_and_reflection_counts(services):
    service, _, admin, _ = services
    task = await _seed_startable_task(service)
    started = await _start(service, task)
    attempt_id = UUID(started["attempt_id"])
    async with admin.transaction() as conn:
        await execute(
            conn,
            """UPDATE presales.proposal_analysis_attempts
            SET execution_stage='reconciling',run_revision=run_revision+1
            WHERE id=:id""",
            id=attempt_id,
        )
        await execute(
            conn,
            """INSERT INTO presales.proposal_checkpoint_receipts
            (attempt_id,execution_generation,run_revision,operation,checkpoint_id,
             write_task_id,checked_at)
            VALUES(:id,1,2,'checkpoint','cp-resume','',now())""",
            id=attempt_id,
        )
        original = (
            await rows(
                conn,
                "SELECT budget,reflection_revision FROM presales.proposal_analysis_attempts"
                " WHERE id=:id",
                id=attempt_id,
            )
        )[0]
    intent = RecoveryIntent(
        request_id=uuid4(),
        attempt_id=attempt_id,
        expected_execution_generation=1,
        expected_run_revision=await _attempt_run_revision(service, attempt_id),
        expected_task_revision=await _task_revision(service, task["task_id"]),
        expected_input_revision=1,
        checkpoint_id="cp-resume",
        input_digest="c" * 64,
    )
    await ProposalAttemptRecovery(service.database).request_recovery(ARCH, intent)
    supervisor = ProposalAttemptSupervisor(service.database, settings=SupervisorSettings())
    claim = await supervisor.claim(attempt_id)
    assert claim.identity.execution_generation == 2
    async with service.database.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt_id,
            )
        )[0]
    assert row["execution_generation"] == 2
    assert row["reflection_revision"] == original["reflection_revision"] == 0
    assert row["budget"] == original["budget"]


@pytest.mark.asyncio
async def test_revoked_actor_cannot_recover(services):
    service, _, admin, _ = services
    task = await _seed_startable_task(service)
    started = await _start(service, task)
    attempt_id = UUID(started["attempt_id"])
    async with admin.transaction() as conn:
        await execute(
            conn,
            """UPDATE presales.proposal_analysis_attempts
            SET execution_stage='reconciling',run_revision=run_revision+1
            WHERE id=:id""",
            id=attempt_id,
        )
        await execute(
            conn,
            "DELETE FROM iam.resource_grants WHERE principal_id=:a AND resource_id=:r",
            a=ARCH.subject,
            r=OPP,
        )
        await execute(
            conn,
            """INSERT INTO presales.proposal_checkpoint_receipts
            (attempt_id,execution_generation,run_revision,operation,checkpoint_id,
             write_task_id,checked_at)
            SELECT id,execution_generation,run_revision,'checkpoint','cp','',now()
            FROM presales.proposal_analysis_attempts WHERE id=:id""",
            id=attempt_id,
        )
    with pytest.raises(Exception) as denied:
        await ProposalAttemptRecovery(service.database).request_recovery(
            ARCH,
            RecoveryIntent(
                request_id=uuid4(),
                attempt_id=attempt_id,
                expected_execution_generation=1,
                expected_run_revision=await _attempt_run_revision(service, attempt_id),
                expected_task_revision=await _task_revision(service, task["task_id"]),
                expected_input_revision=1,
                checkpoint_id="cp",
                input_digest="d" * 64,
            ),
        )
    assert "not_found_or_not_allowed" in str(denied.value)
    async with service.database.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt_id,
            )
        )[0]
    assert row["execution_stage"] == "reconciling"


@pytest.mark.asyncio
async def test_missing_checkpoint_keeps_the_attempt_reconciling(services):
    service, _, admin, _ = services
    task = await _seed_startable_task(service)
    started = await _start(service, task)
    attempt_id = UUID(started["attempt_id"])
    async with admin.transaction() as conn:
        await execute(
            conn,
            """UPDATE presales.proposal_analysis_attempts
            SET execution_stage='reconciling',run_revision=run_revision+1
            WHERE id=:id""",
            id=attempt_id,
        )
    with pytest.raises(Exception) as blocked:
        await ProposalAttemptRecovery(service.database).request_recovery(
            ARCH,
            RecoveryIntent(
                request_id=uuid4(),
                attempt_id=attempt_id,
                expected_execution_generation=1,
                expected_run_revision=await _attempt_run_revision(service, attempt_id),
                expected_task_revision=await _task_revision(service, task["task_id"]),
                expected_input_revision=1,
                checkpoint_id="missing",
                input_digest="e" * 64,
            ),
        )
    assert getattr(blocked.value, "reason", "") == "checkpoint_missing"
    async with service.database.transaction() as conn:
        assert await rows(
            conn,
            "SELECT 1 FROM presales.proposal_task_slots WHERE attempt_id=:id",
            id=attempt_id,
        )


@pytest.mark.asyncio
async def test_supervisor_halts_on_real_connection_loss(services, tmp_path):
    _, _, admin, _ = services
    from presales.infrastructure.config import DatabaseSettings
    from presales.infrastructure.database import Database

    async with admin.transaction() as conn:
        await execute(
            conn,
            """DO $$ BEGIN
              EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', current_database());
            END $$""",
        )
    fresh = Database(
        DatabaseSettings(database_url=os.environ["PRESALES_TEST_RUNTIME_URL"], file_root=tmp_path)
    )
    supervisor = ProposalAttemptSupervisor(
        fresh, settings=SupervisorSettings(heartbeat_seconds=5, lease_seconds=30)
    )
    try:
        await supervisor.start()
        assert supervisor.halted is True
        assert supervisor.accepting is False
        with pytest.raises(StorageUnavailable):
            await supervisor.scan()
        with pytest.raises(StorageUnavailable):
            await supervisor.claim(uuid4())
    finally:
        async with admin.transaction() as conn:
            await execute(
                conn,
                """DO $$ BEGIN
                  EXECUTE format('GRANT CONNECT ON DATABASE %I TO PUBLIC', current_database());
                END $$""",
            )
        await fresh.close()


async def _prepare_draft(admin, task_id: UUID, actor: UUID) -> tuple[UUID, UUID]:
    version, revision = uuid4(), uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO presales.resources VALUES(:id,'proposal',:task)",
            id=version,
            task=task_id,
        )
        await execute(
            conn,
            """INSERT INTO presales.proposal_versions(id,task_id,version_no,state)
            VALUES(:id,:task,1,'draft')""",
            id=version,
            task=task_id,
        )
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,
             input_manifest,created_by)
            VALUES(:id,:version,1,'derived',NULL,false,false,'[]',:editor)""",
            id=revision,
            version=version,
            editor=actor,
        )
        await execute(
            conn,
            """INSERT INTO presales.proposal_revisions
            (id,version_id,revision_no,schema_version,body,editor)
            VALUES(:id,:version,1,'1.0','{}'::jsonb,:editor)""",
            id=revision,
            version=version,
            editor=actor,
        )
    return version, revision


@pytest.mark.asyncio
async def test_successful_commit_releases_slots_and_unblocks_the_next_start(services):
    service, _, admin, _ = services
    first = await _seed_startable_task(service)
    second = await _seed_startable_task(service)
    started = await _start(service, first)
    attempt_id = UUID(started["attempt_id"])
    version, revision = await _prepare_draft(admin, first["task_id"], ARCH.subject)
    supervisor = ProposalAttemptSupervisor(service.database, settings=SupervisorSettings())
    await supervisor.claim(attempt_id)
    async with admin.transaction() as conn:
        row = (
            await rows(
                conn,
                "SELECT run_revision FROM presales.proposal_analysis_attempts WHERE id=:id",
                id=attempt_id,
            )
        )[0]
        await ProposalAttemptStore(conn).commit_result(
            attempt_id,
            expected_generation=1,
            expected_run_revision=row["run_revision"],
            result_revision_id=revision,
            candidate_version_id=version,
            source_snapshot=[first["source"]],
        )
    async with admin.transaction() as conn:
        assert await rows(conn, "SELECT 1 FROM presales.proposal_task_slots") == []
        slot = (await rows(conn, "SELECT * FROM presales.proposal_global_slot"))[0]
    assert slot["attempt_id"] is None
    await _start(service, second)


@pytest.mark.asyncio
async def test_rejected_commit_keeps_slots_occupied(services):
    service, _, admin, _ = services
    task = await _seed_startable_task(service)
    started = await _start(service, task)
    attempt_id = UUID(started["attempt_id"])
    version, revision = await _prepare_draft(admin, task["task_id"], ARCH.subject)
    supervisor = ProposalAttemptSupervisor(service.database, settings=SupervisorSettings())
    await supervisor.claim(attempt_id)
    async with admin.transaction() as conn:
        with pytest.raises(Exception) as error:
            await ProposalAttemptStore(conn).commit_result(
                attempt_id,
                expected_generation=1,
                expected_run_revision=99,
                result_revision_id=revision,
                candidate_version_id=version,
                source_snapshot=[task["source"]],
            )
    assert "revision_or_request_conflict" in str(error.value)
    async with admin.transaction() as conn:
        assert len(await rows(conn, "SELECT 1 FROM presales.proposal_task_slots")) == 1
        slot = (await rows(conn, "SELECT * FROM presales.proposal_global_slot"))[0]
    assert slot["attempt_id"] == attempt_id
