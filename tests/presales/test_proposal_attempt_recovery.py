"""Offline restart-verification contracts; real process races belong to task 12."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from presales.application.proposal_attempt_recovery import (
    ProposalAttemptRecovery,
    RecoveryBlocked,
)
from presales.application.proposal_attempt_runtime import (
    ProposalAttemptSupervisor,
    SupervisorSettings,
)
from presales.domain.contracts import AccessDenied, Actor, Conflict, NotReady, digest
from presales.domain.proposal_analysis_attempt import (
    AttemptBusy,
    AttemptOutcome,
    AttemptTerminal,
    FenceConflict,
    RecoveryIntent,
    transition_attempt,
)

JSON_KEYS = {"body", "budget", "start_scope", "result", "input_manifest"}
UUID_KEYS = {
    "id",
    "actor",
    "task_id",
    "attempt_id",
    "resource_id",
    "result_revision_id",
    "owner",
}
TIME_KEYS = {"lease_until", "cancelled_at", "terminal_at", "created_at", "updated_at"}

TABLE_MAP = {
    "presales.proposal_analysis_attempts": "attempts",
    "presales.proposal_task_slots": "task_slots",
    "presales.proposal_global_slot": "global_slot",
    "presales.proposal_execution_records": "records",
    "presales.proposal_checkpoint_receipts": "checkpoint_receipts",
    "presales.tasks": "tasks",
    "presales.resources": "resources",
    "presales.resource_versions": "resource_versions",
    "presales.knowledge_versions": "knowledge_versions",
    "presales.direct_dependencies": "direct_dependencies",
    "presales.material_versions": "material_versions",
    "presales.file_readiness": "file_readiness",
    "iam.principals": "principals",
    "iam.resource_grants": "resource_grants",
    "iam.authorization_revision": "authorization_revision",
    "audit.events": "events",
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Result:
    def __init__(self, cursor) -> None:
        self.rowcount = cursor.rowcount
        self.values = []
        for row in cursor.fetchall():
            value = dict(row)
            for key, item in value.items():
                if item is not None and key in JSON_KEYS:
                    value[key] = json.loads(item)
                elif item and key in UUID_KEYS:
                    value[key] = UUID(item)
                elif item and key in TIME_KEYS:
                    value[key] = datetime.fromisoformat(item)
            self.values.append(value)

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.values)

    def first(self):
        return next(iter(self.values), None)

    def one(self):
        assert len(self.values) == 1
        return self.values[0]


class OfflineDatabase:
    def __init__(self, root) -> None:
        self.root = Path(root)

    @asynccontextmanager
    async def transaction(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for schema in ("presales", "iam", "audit"):
            conn.execute(f"ATTACH DATABASE ? AS {schema}", (str(self.root / f"{schema}.db"),))

        class Connection:
            async def execute(self, statement, params=None):
                sql = str(statement)
                for qualified, table in TABLE_MAP.items():
                    sql = sql.replace(qualified, table)
                sql = " ".join(sql.split())
                for clause in (" FOR UPDATE", " FOR SHARE", " FOR UPDATE OF a", " FOR SHARE OF a"):
                    sql = sql.replace(clause, "")
                sql = sql.replace("singleton=true", "singleton=1")
                sql = sql.replace("now()", "CURRENT_TIMESTAMP")
                sql = sql.replace("CAST(:body AS jsonb)", ":body")
                sql = sql.replace("CAST(:result AS jsonb)", ":result")
                sql = sql.replace("CAST(:budget AS jsonb)", ":budget")
                sql = sql.replace("CAST(:start_scope AS jsonb)", ":start_scope")
                values = {
                    key: str(value) if isinstance(value, UUID | datetime) else value
                    for key, value in (params or {}).items()
                }
                if sql.startswith("SELECT presales.resume_proposal_attempt"):
                    try:
                        resumed = _resume(conn, values)
                    except sqlite3.IntegrityError as error:
                        raise _recovery_error(str(error)) from None
                    return Result(conn.execute("SELECT ? AS resume_proposal_attempt", (resumed,)))
                return Result(conn.execute(sql, values))

        try:
            yield Connection()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


_RESUME_ERRORS = {
    "not_found_or_not_allowed": AccessDenied,
    "attempt_terminal": AttemptTerminal,
    "not_ready": NotReady,
    "fence_conflict": FenceConflict,
    "revision_or_request_conflict": Conflict,
    "recovery_blocked": RecoveryBlocked,
}


def _recovery_error(code: str):
    error = _RESUME_ERRORS.get(code)
    if error is None:
        return sqlite3.IntegrityError(code)
    if error is RecoveryBlocked:
        return RecoveryBlocked(code)
    return error()


def _resume(conn, params):
    attempt = params["attempt"]
    generation = params["generation"]
    row = conn.execute(
        "SELECT execution_stage,cancelled_at,execution_generation,run_revision,"
        "input_revision,actor FROM attempts WHERE id=?",
        (attempt,),
    ).fetchone()
    task = conn.execute(
        "SELECT state,revision,input_revision FROM tasks WHERE id=("
        "SELECT task_id FROM attempts WHERE id=?)",
        (attempt,),
    ).fetchone()
    if row is None or row[5] != params["actor"]:
        raise sqlite3.IntegrityError("not_found_or_not_allowed")
    if row[0] == "finished" or row[1] is not None or task[0] == "cancelled":
        raise sqlite3.IntegrityError("attempt_terminal")
    if row[0] != "reconciling":
        raise sqlite3.IntegrityError("not_ready")
    if row[2] != generation:
        raise sqlite3.IntegrityError("fence_conflict")
    if row[3] != params["run_revision"] or task[1] != params["task_revision"]:
        raise sqlite3.IntegrityError("revision_or_request_conflict")
    if row[4] != params["input_revision"] or task[2] != params["input_revision"]:
        raise sqlite3.IntegrityError("invalid_input")
    intent = conn.execute(
        "SELECT 1 FROM records WHERE attempt_id=? AND kind='recovery' AND record_key=? "
        "AND execution_generation=? AND status='completed'",
        (attempt, f"recovery-intent-{params['request']}", generation),
    ).fetchone()
    if intent is None:
        raise sqlite3.IntegrityError("recovery_blocked")
    if conn.execute(
        "SELECT 1 FROM records WHERE attempt_id=? AND kind='call' "
        "AND status IN ('registered','unknown')",
        (attempt,),
    ).fetchone():
        raise sqlite3.IntegrityError("recovery_blocked")
    for sql in (
        "UPDATE task_slots SET execution_generation=? WHERE attempt_id=? AND execution_generation=?",
        "UPDATE global_slot SET execution_generation=? WHERE attempt_id=? "
        "AND execution_generation=?",
    ):
        if conn.execute(sql, (generation + 1, attempt, generation)).rowcount != 1:
            raise sqlite3.IntegrityError("fence_conflict")
    if (
        conn.execute(
            "UPDATE attempts SET execution_generation=?,execution_stage='running',"
            "run_revision=run_revision+1 WHERE id=? AND execution_generation=? "
            "AND execution_stage='reconciling'",
            (generation + 1, attempt, generation),
        ).rowcount
        != 1
    ):
        raise sqlite3.IntegrityError("fence_conflict")
    return generation + 1


class Fixture:
    def __init__(self, root, actor, task, attempt, source) -> None:
        self.root, self.actor, self.task = Path(root), actor, task
        self.attempt, self.source = attempt, source

    def connect(self):
        conn = sqlite3.connect(self.root / "presales.db")
        conn.row_factory = sqlite3.Row
        return conn

    def attempt_row(self):
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM attempts").fetchone())

    def task_row(self):
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM tasks").fetchone())

    def slots(self):
        with self.connect() as conn:
            task = conn.execute("SELECT * FROM task_slots").fetchone()
            global_slot = conn.execute("SELECT * FROM global_slot").fetchone()
        return (dict(task) if task else None, dict(global_slot) if global_slot else None)

    def records(self, kind=None):
        with self.connect() as conn:
            values = [dict(row) for row in conn.execute("SELECT * FROM records")]
        return [row for row in values if kind is None or row["kind"] == kind]

    def execute(self, schema, sql, params=()):
        with sqlite3.connect(self.root / f"{schema}.db") as conn:
            conn.execute(sql, params)

    def intent(self, **overrides):
        attempt = self.attempt_row()
        values = {
            "request_id": uuid4(),
            "attempt_id": self.attempt,
            "expected_execution_generation": attempt["execution_generation"],
            "expected_run_revision": attempt["run_revision"],
            "expected_task_revision": 2,
            "expected_input_revision": 1,
            "checkpoint_id": "checkpoint-1",
            "input_digest": digest({"attempt": str(self.attempt)}),
        }
        values.update(overrides)
        return RecoveryIntent(**values)


def build(tmp_path, monkeypatch, *, stage="reconciling", task_state="analyzing"):
    async def no_lock(*args, **kwargs):
        pass

    monkeypatch.setattr("presales.access.policy.lock", no_lock)
    monkeypatch.setattr("presales.application.proposal_attempt_recovery.lock", no_lock)
    tmp_path.mkdir(parents=True, exist_ok=True)
    actor, task, attempt, source = uuid4(), uuid4(), uuid4(), uuid4()
    clock = Clock()
    lease = (clock() + timedelta(seconds=30)).isoformat()
    presales = sqlite3.connect(tmp_path / "presales.db")
    presales.executescript(
        """
        CREATE TABLE resources(id TEXT,parent_id TEXT,kind TEXT);
        CREATE TABLE tasks(id TEXT,state TEXT,revision INTEGER,input_revision INTEGER);
        CREATE TABLE attempts(id TEXT PRIMARY KEY,task_id TEXT,actor TEXT,thread_id TEXT,
            input_revision INTEGER,start_scope TEXT,model_mode TEXT,config_identity TEXT,
            execution_generation INTEGER,execution_stage TEXT,outcome TEXT,run_revision INTEGER,
            result_ready INTEGER DEFAULT 0,result_revision_id TEXT,candidate_version_id TEXT,
            source_snapshot TEXT,cancelled_at TEXT,terminal_at TEXT,error_code TEXT,budget TEXT,
            reflection_revision INTEGER DEFAULT 0,created_at TEXT,updated_at TEXT);
        CREATE TABLE task_slots(task_id TEXT PRIMARY KEY,attempt_id TEXT,
            execution_generation INTEGER,owner TEXT,lease_until TEXT);
        CREATE TABLE global_slot(singleton INTEGER PRIMARY KEY,attempt_id TEXT,
            execution_generation INTEGER,owner TEXT,lease_until TEXT);
        CREATE TABLE records(attempt_id TEXT,kind TEXT,record_key TEXT,
            execution_generation INTEGER,input_digest TEXT,status TEXT,body TEXT,
            result_revision_id TEXT,PRIMARY KEY(attempt_id,kind,record_key));
        CREATE TABLE command_receipts(actor TEXT,operation TEXT,request_id TEXT,
            parameter_digest TEXT,result TEXT,PRIMARY KEY(actor,operation,request_id));
        CREATE TABLE checkpoint_receipts(attempt_id TEXT,execution_generation INTEGER,
            run_revision INTEGER,operation TEXT,checkpoint_id TEXT,write_task_id TEXT,
            checked_at TEXT,PRIMARY KEY(attempt_id,execution_generation,operation,
            checkpoint_id,write_task_id));
        CREATE TABLE resource_versions(id TEXT,resource_id TEXT,ready INTEGER,
            dependencies_complete INTEGER,input_manifest TEXT,source_kind TEXT,declaration TEXT);
        CREATE TABLE knowledge_versions(id TEXT,state TEXT);
        CREATE TABLE direct_dependencies(downstream_id TEXT,upstream_id TEXT);
        CREATE TABLE material_versions(id TEXT);
        CREATE TABLE file_readiness(id TEXT,state TEXT);
        """
    )
    presales.execute("INSERT INTO resources VALUES(?,NULL,'task')", (str(task),))
    presales.execute("INSERT INTO tasks VALUES(?,?,2,1)", (str(task), task_state))
    presales.execute(
        "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,NULL,2,0,NULL,NULL,NULL,NULL,NULL,"
        "NULL,?,0,'2026-11-12T09:00:00+00:00','2026-11-12T09:00:00+00:00')",
        (
            str(attempt),
            str(task),
            str(actor),
            f"attempt-{attempt}",
            1,
            json.dumps([str(source)]),
            "fake",
            "config",
            1,
            stage,
            json.dumps({"max_active_seconds": 600}),
        ),
    )
    presales.execute(
        "INSERT INTO task_slots VALUES(?,?,1,?,?)",
        (str(task), str(attempt), str(actor), lease),
    )
    presales.execute(
        "INSERT INTO global_slot VALUES(1,?,1,?,?)",
        (str(attempt), str(actor), lease),
    )
    presales.execute(
        "INSERT INTO resource_versions VALUES(?,?,1,1,'[]','original','synthetic')",
        (str(source), str(task)),
    )
    presales.execute(
        "INSERT INTO checkpoint_receipts VALUES(?,1,2,'checkpoint','checkpoint-1','',"
        "'2026-11-12T09:00:00+00:00')",
        (str(attempt),),
    )
    presales.commit()
    presales.close()
    iam = sqlite3.connect(tmp_path / "iam.db")
    iam.executescript(
        """CREATE TABLE principals(id TEXT,role TEXT,active INTEGER);
        CREATE TABLE authorization_revision(revision INTEGER);
        CREATE TABLE resource_grants(principal_id TEXT,resource_id TEXT);"""
    )
    iam.execute("INSERT INTO principals VALUES(?,?,1)", (str(actor), "architect"))
    iam.execute("INSERT INTO authorization_revision VALUES(1)")
    iam.execute("INSERT INTO resource_grants VALUES(?,?)", (str(actor), str(task)))
    iam.commit()
    iam.close()
    audit = sqlite3.connect(tmp_path / "audit.db")
    audit.executescript(
        """CREATE TABLE events(id TEXT,actor TEXT,operation TEXT,target TEXT,revision INTEGER,
        result TEXT,request_id TEXT,authorization_revision INTEGER);"""
    )
    audit.commit()
    audit.close()
    return Fixture(tmp_path, actor, task, attempt, source)


def service(fixture):
    return ProposalAttemptRecovery(OfflineDatabase(fixture.root), clock=Clock())


@pytest.mark.asyncio
async def test_inspect_classifies_reconciling_attempt_as_awaiting_verification(
    tmp_path, monkeypatch
):
    fixture = build(tmp_path, monkeypatch)
    finding = await service(fixture).inspect(Actor(subject=fixture.actor), fixture.attempt)
    assert finding.code == "awaiting_verification"
    assert finding.open_calls == ()
    assert finding.stage.value == "reconciling"
    assert fixture.records(kind="recovery") == []
    assert fixture.attempt_row()["execution_stage"] == "reconciling"


@pytest.mark.asyncio
async def test_inspect_reports_terminal_and_business_committed_and_open_calls(
    tmp_path, monkeypatch
):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute(
        "presales",
        "UPDATE attempts SET execution_stage='finished',outcome='failed',terminal_at=?",
        ("2026-11-12T09:10:00+00:00",),
    )
    terminal = await service(fixture).inspect(Actor(subject=fixture.actor), fixture.attempt)
    assert terminal.code == "terminal"
    assert fixture.attempt_row()["execution_stage"] == "finished"


@pytest.mark.asyncio
async def test_inspect_reports_business_committed_without_reopening(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute("presales", "UPDATE attempts SET result_ready=1")
    finding = await service(fixture).inspect(Actor(subject=fixture.actor), fixture.attempt)
    assert finding.code == "business_committed"
    assert fixture.attempt_row()["execution_stage"] == "reconciling"


@pytest.mark.asyncio
async def test_inspect_reports_open_calls_and_never_retries(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute(
        "presales",
        "INSERT INTO records VALUES(?,?,?,1,?,?,?,'')",
        (
            str(fixture.attempt),
            "call",
            "call-1",
            digest({"call": 1}),
            "unknown",
            json.dumps({"type": "model", "node": "requirement"}),
        ),
    )
    finding = await service(fixture).inspect(Actor(subject=fixture.actor), fixture.attempt)
    assert finding.code == "unknown_calls_pending"
    assert finding.open_calls == ("call-1",)
    assert len(fixture.records(kind="call")) == 1


@pytest.mark.asyncio
async def test_recovery_records_stable_intent_and_replays_the_same_result(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    intent = fixture.intent()
    first = await service(fixture).request_recovery(Actor(subject=fixture.actor), intent)
    second = await service(fixture).request_recovery(Actor(subject=fixture.actor), intent)
    assert first == second
    assert first["resume_permitted"] is True
    assert first["next_execution_generation"] == 2
    assert first["remaining_active_seconds"] == 600
    intents = [
        row
        for row in fixture.records(kind="recovery")
        if row["record_key"].startswith("recovery-intent-")
    ]
    assert len(intents) == 1
    assert fixture.attempt_row()["execution_stage"] == "reconciling"
    assert fixture.attempt_row()["execution_generation"] == 1


@pytest.mark.asyncio
async def test_inspect_reports_recorded_intent_without_starting_execution(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    await service(fixture).request_recovery(Actor(subject=fixture.actor), fixture.intent())
    finding = await service(fixture).inspect(Actor(subject=fixture.actor), fixture.attempt)
    assert finding.code == "resume_recorded"
    assert fixture.attempt_row()["execution_stage"] == "reconciling"
    assert fixture.attempt_row()["execution_generation"] == 1


@pytest.mark.asyncio
async def test_recovery_rejects_same_request_id_with_different_parameters(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    intent = fixture.intent()
    await service(fixture).request_recovery(Actor(subject=fixture.actor), intent)
    conflicting = intent.model_copy(update={"checkpoint_id": "checkpoint-2"})
    with pytest.raises(Conflict) as error:
        await service(fixture).request_recovery(Actor(subject=fixture.actor), conflicting)
    assert error.value.code == "revision_or_request_conflict"
    assert len(fixture.records(kind="recovery")) == 1


@pytest.mark.asyncio
async def test_foreign_actor_cannot_inspect_or_recover(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    stranger = Actor(subject=uuid4())
    with pytest.raises(AccessDenied) as inspect_error:
        await service(fixture).inspect(stranger, fixture.attempt)
    with pytest.raises(AccessDenied) as recover_error:
        await service(fixture).request_recovery(stranger, fixture.intent())
    assert inspect_error.value.code == "not_found_or_not_allowed"
    assert recover_error.value.code == "not_found_or_not_allowed"
    assert fixture.records(kind="recovery") == []
    assert fixture.attempt_row()["execution_generation"] == 1


@pytest.mark.asyncio
async def test_terminal_or_cancelled_attempt_cannot_be_recovered(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute(
        "presales",
        "UPDATE attempts SET cancelled_at=?,execution_stage='finished',outcome='cancelled',"
        "terminal_at=?",
        ("2026-11-12T09:10:00+00:00", "2026-11-12T09:10:00+00:00"),
    )
    with pytest.raises(AttemptTerminal) as error:
        await service(fixture).request_recovery(Actor(subject=fixture.actor), fixture.intent())
    assert error.value.code == "attempt_terminal"
    assert fixture.records(kind="recovery") == []


@pytest.mark.asyncio
async def test_recovery_requires_reconciling_stage_and_rejects_stale_identity(
    tmp_path, monkeypatch
):
    running = build(tmp_path, monkeypatch)
    running.execute("presales", "UPDATE attempts SET execution_stage='running'")
    with pytest.raises(NotReady) as stage_error:
        await service(running).request_recovery(Actor(subject=running.actor), running.intent())
    assert stage_error.value.code == "not_ready"
    assert running.records(kind="recovery") == []

    other = build(tmp_path / "stale", monkeypatch)
    with pytest.raises(FenceConflict) as generation_error:
        await service(other).request_recovery(
            Actor(subject=other.actor),
            other.intent(expected_execution_generation=2),
        )
    assert generation_error.value.code == "fence_conflict"
    with pytest.raises(Conflict) as revision_error:
        await service(other).request_recovery(
            Actor(subject=other.actor), other.intent(expected_run_revision=7)
        )
    assert revision_error.value.code == "revision_or_request_conflict"
    assert other.records(kind="recovery") == []
    assert other.attempt_row()["execution_generation"] == 1


@pytest.mark.asyncio
async def test_recovery_rejects_changed_input_or_task_revision(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(RecoveryBlocked) as input_error:
        await service(fixture).request_recovery(
            Actor(subject=fixture.actor), fixture.intent(expected_input_revision=2)
        )
    assert input_error.value.code == "recovery_blocked"
    assert input_error.value.reason == "input_changed"
    with pytest.raises(Conflict) as task_error:
        await service(fixture).request_recovery(
            Actor(subject=fixture.actor), fixture.intent(expected_task_revision=3)
        )
    assert task_error.value.code == "revision_or_request_conflict"
    assert fixture.records(kind="recovery") == []


@pytest.mark.asyncio
async def test_recovery_rejects_missing_checkpoint_and_exhausted_budget(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(RecoveryBlocked) as checkpoint_error:
        await service(fixture).request_recovery(
            Actor(subject=fixture.actor), fixture.intent(checkpoint_id="missing")
        )
    assert checkpoint_error.value.reason == "checkpoint_missing"
    fixture.execute(
        "presales",
        "INSERT INTO records VALUES(?,?,?,1,?,?,'{}','')",
        (
            str(fixture.attempt),
            "recovery",
            "active-spent",
            digest({"active": "spent"}),
            "completed",
        ),
    )
    fixture.execute(
        "presales",
        "UPDATE records SET body=? WHERE record_key='active-spent'",
        (json.dumps({"type": "active", "start": 0.0, "deadline": 600.0, "end": 600.0}),),
    )
    with pytest.raises(RecoveryBlocked) as budget_error:
        await service(fixture).request_recovery(Actor(subject=fixture.actor), fixture.intent())
    assert budget_error.value.reason == "budget_exhausted"
    assert fixture.attempt_row()["execution_stage"] == "reconciling"


@pytest.mark.asyncio
async def test_recovery_rejects_open_call_and_business_committed(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute(
        "presales",
        "INSERT INTO records VALUES(?,?,?,1,?,?,'{}','')",
        (str(fixture.attempt), "call", "call-9", digest({"call": 9}), "registered"),
    )
    with pytest.raises(RecoveryBlocked) as call_error:
        await service(fixture).request_recovery(Actor(subject=fixture.actor), fixture.intent())
    assert call_error.value.reason == "unknown_calls_pending"
    assert len(fixture.records(kind="call")) == 1
    fixture.execute("presales", "UPDATE records SET status='completed' WHERE record_key='call-9'")
    fixture.execute("presales", "UPDATE attempts SET result_ready=1")
    with pytest.raises(RecoveryBlocked) as committed_error:
        await service(fixture).request_recovery(Actor(subject=fixture.actor), fixture.intent())
    assert committed_error.value.reason == "business_already_committed"
    assert len(fixture.records(kind="call")) == 1


@pytest.mark.asyncio
async def test_recovery_rejects_source_that_is_no_longer_published(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute(
        "presales",
        "INSERT INTO knowledge_versions VALUES(?,'retired')",
        (str(fixture.source),),
    )
    with pytest.raises(RecoveryBlocked) as error:
        await service(fixture).request_recovery(Actor(subject=fixture.actor), fixture.intent())
    assert error.value.reason == "source_invalid"
    assert fixture.records(kind="recovery") == []


@pytest.mark.asyncio
async def test_close_unrecoverable_finishes_attempt_and_releases_slots(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    result = await service(fixture).close_unrecoverable(
        Actor(subject=fixture.actor),
        fixture.attempt,
        request_id=uuid4(),
        execution_stopped=True,
        reason="checkpoint_missing",
    )
    assert result["stage"] == "finished"
    assert result["outcome"] == "failed"
    assert result["slots_released"] is True
    assert fixture.attempt_row()["execution_stage"] == "finished"
    assert fixture.attempt_row()["outcome"] == "failed"
    assert fixture.slots()[0] is None
    assert fixture.slots()[1]["attempt_id"] is None


@pytest.mark.asyncio
async def test_close_unrecoverable_waits_for_stop_and_keeps_slots(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(RecoveryBlocked) as stop_error:
        await service(fixture).close_unrecoverable(
            Actor(subject=fixture.actor),
            fixture.attempt,
            request_id=uuid4(),
            execution_stopped=False,
            reason="checkpoint_missing",
        )
    assert stop_error.value.reason == "execution_unconfirmed"
    fixture.execute(
        "presales",
        "INSERT INTO records VALUES(?,?,?,1,?,?,'{}','')",
        (str(fixture.attempt), "call", "call-7", digest({"call": 7}), "unknown"),
    )
    with pytest.raises(RecoveryBlocked) as call_error:
        await service(fixture).close_unrecoverable(
            Actor(subject=fixture.actor),
            fixture.attempt,
            request_id=uuid4(),
            execution_stopped=True,
            reason="checkpoint_missing",
        )
    assert call_error.value.reason == "execution_unconfirmed"
    assert fixture.attempt_row()["execution_stage"] == "reconciling"
    assert fixture.slots()[0] is not None


@pytest.mark.asyncio
async def test_close_unrecoverable_refuses_terminal_and_non_reconciling(tmp_path, monkeypatch):
    terminal = build(tmp_path, monkeypatch)
    terminal.execute(
        "presales",
        "UPDATE attempts SET execution_stage='finished',outcome='failed',terminal_at=?",
        ("2026-11-12T09:10:00+00:00",),
    )
    with pytest.raises(AttemptTerminal) as terminal_error:
        await service(terminal).close_unrecoverable(
            Actor(subject=terminal.actor),
            terminal.attempt,
            request_id=uuid4(),
            execution_stopped=True,
            reason="checkpoint_missing",
        )
    assert terminal_error.value.code == "attempt_terminal"

    running = build(tmp_path / "running", monkeypatch)
    running.execute("presales", "UPDATE attempts SET execution_stage='running'")
    with pytest.raises(NotReady) as stage_error:
        await service(running).close_unrecoverable(
            Actor(subject=running.actor),
            running.attempt,
            request_id=uuid4(),
            execution_stopped=True,
            reason="checkpoint_missing",
        )
    assert stage_error.value.code == "not_ready"
    assert running.slots()[0] is not None


@pytest.mark.asyncio
async def test_close_unrecoverable_rejects_success_outcome(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        await service(fixture).close_unrecoverable(
            Actor(subject=fixture.actor),
            fixture.attempt,
            request_id=uuid4(),
            execution_stopped=True,
            reason="checkpoint_missing",
            outcome=AttemptOutcome.SUCCEEDED,
        )
    assert fixture.attempt_row()["execution_stage"] == "reconciling"


def supervisor(fixture, *, runner=None):
    return ProposalAttemptSupervisor(
        OfflineDatabase(fixture.root),
        runner=runner,
        settings=SupervisorSettings(heartbeat_seconds=5, lease_seconds=30, shutdown_seconds=2),
        clock=Clock(),
    )


class RecordingRunner:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, identity):
        self.calls.append(identity)


@pytest.mark.asyncio
async def test_recorded_intent_lets_the_supervisor_resume_with_the_next_generation(
    tmp_path, monkeypatch
):
    fixture = build(tmp_path, monkeypatch)
    actor = Actor(subject=fixture.actor)
    intent = fixture.intent()
    await service(fixture).request_recovery(actor, intent)
    runner = RecordingRunner()
    instance = supervisor(fixture, runner=runner)
    await instance.start()
    try:
        claim = await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert claim.identity.execution_generation == 2
    assert claim.identity.thread_id == f"attempt-{fixture.attempt}"
    assert claim.run_revision == 3
    row = fixture.attempt_row()
    assert (row["execution_stage"], row["execution_generation"]) == ("running", 2)
    task_slot, global_slot = fixture.slots()
    assert (task_slot["execution_generation"], global_slot["execution_generation"]) == (2, 2)
    assert datetime.fromisoformat(global_slot["lease_until"]) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_second_resume_without_a_new_intent_is_refused(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    actor = Actor(subject=fixture.actor)
    await service(fixture).request_recovery(actor, fixture.intent())
    instance = supervisor(fixture, runner=RecordingRunner())
    await instance.start()
    try:
        await instance.claim(fixture.attempt)
        with pytest.raises(AttemptBusy) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "attempt_busy"
    assert fixture.attempt_row()["execution_generation"] == 2


@pytest.mark.asyncio
async def test_cancelled_reconciling_attempt_cannot_be_resumed(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    actor = Actor(subject=fixture.actor)
    await service(fixture).request_recovery(actor, fixture.intent())
    fixture.execute(
        "presales",
        "UPDATE attempts SET cancelled_at=?",
        ("2026-11-12T09:30:00+00:00",),
    )
    instance = supervisor(fixture, runner=RecordingRunner())
    await instance.start()
    try:
        with pytest.raises(AttemptTerminal) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "attempt_terminal"
    row = fixture.attempt_row()
    assert (row["execution_stage"], row["execution_generation"]) == ("reconciling", 1)


@pytest.mark.asyncio
async def test_resume_is_fenced_when_a_slot_is_not_held_by_this_generation(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    actor = Actor(subject=fixture.actor)
    await service(fixture).request_recovery(actor, fixture.intent())
    fixture.execute(
        "presales",
        "UPDATE global_slot SET attempt_id=?",
        (str(uuid4()),),
    )
    instance = supervisor(fixture, runner=RecordingRunner())
    await instance.start()
    try:
        with pytest.raises(FenceConflict) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "fence_conflict"
    row = fixture.attempt_row()
    assert (row["execution_stage"], row["execution_generation"]) == ("reconciling", 1)


def test_domain_resume_requires_the_next_generation_exactly():
    from presales.domain.proposal_analysis_attempt import ProposalAnalysisAttempt

    attempt = ProposalAnalysisAttempt(
        id=uuid4(),
        task_id=uuid4(),
        actor_id=uuid4(),
        thread_id="attempt-x",
        input_revision=1,
        start_scope=(uuid4(),),
        model_mode="fake",
        config_identity="config",
        execution_generation=1,
        stage="reconciling",
        run_revision=5,
    )
    resumed = transition_attempt(attempt, "running", resumed_generation=2)
    assert (str(resumed.stage), resumed.execution_generation) == ("running", 2)
    assert resumed.run_revision == 6
    with pytest.raises(Conflict) as error:
        transition_attempt(attempt, "running", resumed_generation=3)
    assert error.value.code == "revision_or_request_conflict"
    with pytest.raises(Conflict):
        transition_attempt(attempt, "running")
    terminal = attempt.model_copy(
        update={
            "stage": "finished",
            "outcome": "failed",
            "terminal_at": datetime(2026, 11, 12, 9, 30, tzinfo=UTC),
            "execution_generation": 2,
            "run_revision": 6,
        }
    )
    with pytest.raises(AttemptTerminal):
        transition_attempt(terminal, "running", resumed_generation=3)
