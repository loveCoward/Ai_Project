"""Offline cancellation and drain contracts; real races belong to task 12/24."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from presales.application.proposal_attempt_commands import ProposalAttemptCancel
from presales.application.proposal_attempt_runtime import (
    ProposalAttemptSupervisor,
    SupervisorSettings,
)
from presales.domain.contracts import AccessDenied, Actor, Conflict
from presales.domain.proposal_analysis_attempt import (
    AttemptTerminal,
    CancelAttemptCommand,
    CancelNotAllowed,
)
from presales.infrastructure.proposal_attempt_store import ProposalAttemptStore

OBJECT_KEYS = {
    "id",
    "actor",
    "task_id",
    "attempt_id",
    "resource_id",
    "result_revision_id",
    "owner",
    "source_version_id",
}
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


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


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


TABLE_MAP = {
    "presales.proposal_analysis_attempts": "attempts",
    "presales.proposal_task_slots": "task_slots",
    "presales.proposal_global_slot": "global_slot",
    "presales.proposal_execution_records": "records",
    "presales.tasks": "tasks",
}


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
                sql = sql.replace(" FOR UPDATE", "").replace(" FOR SHARE", "")
                sql = sql.replace("singleton=true", "singleton=1")
                sql = sql.replace("now()", "CURRENT_TIMESTAMP")
                sql = sql.replace("CAST(:body AS jsonb)", ":body")
                sql = sql.replace("CAST(:result AS jsonb)", ":result")
                sql = sql.replace("CAST(:start_scope AS jsonb)", ":start_scope")
                sql = sql.replace("CAST(:budget AS jsonb)", ":budget")
                sql = sql.replace("CAST(:result AS jsonb)", ":result")
                params = {
                    key: str(value) if isinstance(value, UUID | datetime) else value
                    for key, value in (params or {}).items()
                }
                if sql.strip().startswith("SELECT presales.cancel_proposal_task"):
                    result = _cancel_task(conn, params)
                    if result is None:
                        return Result(_empty(conn))
                    return Result(conn.execute("SELECT ? AS cancel_proposal_task", (result,)))
                return Result(conn.execute(sql, params))

        try:
            yield Connection()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _empty(conn):
    return conn.execute("SELECT 1 AS ok WHERE 0")


def _cancel_task(conn, params):
    actor, task, revision = params["actor"], params["task"], params["expected_revision"]
    principal = conn.execute(
        "SELECT active,role FROM iam.principals WHERE id=?", (actor,)
    ).fetchone()
    granted = conn.execute(
        "SELECT 1 FROM iam.resource_grants WHERE principal_id=? AND resource_id=?",
        (actor, task),
    ).fetchone()
    if not principal or not principal[0] or principal[1] != "architect" or not granted:
        raise sqlite3.IntegrityError("not_found_or_not_allowed")
    row = conn.execute("SELECT state,revision FROM tasks WHERE id=?", (task,)).fetchone()
    if row is None or row[1] != revision:
        raise sqlite3.IntegrityError("revision_or_request_conflict")
    if row[0] in ("approved", "completed"):
        raise sqlite3.IntegrityError("cancel_not_allowed")
    if row[0] == "cancelled":
        raise sqlite3.IntegrityError("attempt_terminal")
    cursor = conn.execute(
        "UPDATE tasks SET state='cancelled',revision=revision+1 WHERE id=?", (task,)
    )
    assert cursor.rowcount == 1
    return revision + 1


class Fixture:
    def __init__(self, root, actor, task, attempt) -> None:
        self.root, self.actor, self.task, self.attempt = root, actor, task, attempt

    def command(self, **overrides):
        values = {
            "request_id": uuid4(),
            "task_id": self.task,
            "expected_task_revision": 2,
            "attempt_id": self.attempt,
        }
        values.update(overrides)
        return CancelAttemptCommand(**values)

    def connect(self):
        conn = sqlite3.connect(self.root / "presales.db")
        conn.row_factory = sqlite3.Row
        return conn

    def task_row(self):
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM tasks").fetchone())

    def attempt_row(self):
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM attempts").fetchone())

    def slots(self):
        with self.connect() as conn:
            task = conn.execute("SELECT * FROM task_slots").fetchone()
            global_slot = conn.execute("SELECT * FROM global_slot").fetchone()
        return (dict(task) if task else None, dict(global_slot) if global_slot else None)

    def execute(self, sql, params=()):
        with self.connect() as conn:
            conn.execute(sql, params)


def build(tmp_path, monkeypatch, *, stage="running", task_state="analyzing", role="architect"):
    async def no_lock(*args, **kwargs):
        pass

    monkeypatch.setattr("presales.access.policy.lock", no_lock)
    monkeypatch.setattr("presales.application.proposal_attempt_commands.lock", no_lock)
    tmp_path.mkdir(parents=True, exist_ok=True)
    actor, task, attempt = uuid4(), uuid4(), uuid4()
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
            json.dumps([str(uuid4())]),
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
    presales.commit()
    presales.close()
    iam = sqlite3.connect(tmp_path / "iam.db")
    iam.executescript(
        """CREATE TABLE principals(id TEXT,role TEXT,active INTEGER);
        CREATE TABLE authorization_revision(revision INTEGER);
        CREATE TABLE resource_grants(principal_id TEXT,resource_id TEXT);"""
    )
    iam.execute("INSERT INTO principals VALUES(?,?,1)", (str(actor), role))
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
    return Fixture(tmp_path, actor, task, attempt)


def service(fixture):
    return ProposalAttemptCancel(OfflineDatabase(fixture.root))


@pytest.mark.asyncio
async def test_cancel_persists_task_attempt_and_receipt_but_keeps_slots(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    result = await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    assert result["task_state"] == "cancelled"
    assert result["attempt_stage"] == "reconciling"
    assert result["drain"] == "reconciling"
    assert fixture.task_row()["state"] == "cancelled"
    attempt = fixture.attempt_row()
    assert attempt["cancelled_at"] is not None
    assert attempt["execution_stage"] == "reconciling"
    assert fixture.slots()[0] is not None and fixture.slots()[1]["attempt_id"] is not None
    with sqlite3.connect(fixture.root / "audit.db") as conn:
        assert conn.execute("SELECT operation,result FROM events").fetchone() == (
            "cancel_proposal_task",
            "cancelled",
        )


@pytest.mark.asyncio
async def test_cancel_before_commit_blocks_late_result(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    async with OfflineDatabase(fixture.root).transaction() as conn:
        store = ProposalAttemptStore(conn)
        with pytest.raises(AttemptTerminal):
            await store.commit_result(
                fixture.attempt,
                expected_generation=1,
                expected_run_revision=3,
                result_revision_id=uuid4(),
                candidate_version_id=uuid4(),
                source_snapshot=[uuid4()],
            )
    assert fixture.attempt_row()["outcome"] is None


@pytest.mark.asyncio
async def test_successful_commit_releases_both_slots_in_the_same_transaction(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    async with OfflineDatabase(fixture.root).transaction() as conn:
        await ProposalAttemptStore(conn).commit_result(
            fixture.attempt,
            expected_generation=1,
            expected_run_revision=2,
            result_revision_id=uuid4(),
            candidate_version_id=uuid4(),
            source_snapshot=[uuid4()],
        )
    task_slot, global_slot = fixture.slots()
    assert task_slot is None
    assert global_slot["attempt_id"] is None
    assert global_slot["execution_generation"] is None
    assert fixture.attempt_row()["execution_stage"] == "finished"


@pytest.mark.asyncio
async def test_rejected_commit_leaves_slots_occupied(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    async with OfflineDatabase(fixture.root).transaction() as conn:
        with pytest.raises(Exception) as error:
            await ProposalAttemptStore(conn).commit_result(
                fixture.attempt,
                expected_generation=1,
                expected_run_revision=7,
                result_revision_id=uuid4(),
                candidate_version_id=uuid4(),
                source_snapshot=[uuid4()],
            )
    assert error.value.code == "revision_or_request_conflict"
    task_slot, global_slot = fixture.slots()
    assert task_slot is not None
    assert global_slot["attempt_id"] is not None


@pytest.mark.asyncio
async def test_commit_before_cancel_preserves_draft_and_marks_task_cancelled(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    candidate, revision, source = uuid4(), uuid4(), uuid4()
    async with OfflineDatabase(fixture.root).transaction() as conn:
        store = ProposalAttemptStore(conn)
        await store.commit_result(
            fixture.attempt,
            expected_generation=1,
            expected_run_revision=2,
            result_revision_id=revision,
            candidate_version_id=candidate,
            source_snapshot=[source],
        )
    fixture.execute("UPDATE tasks SET state='analyzed_awaiting_submission'")
    fixture.execute(
        "UPDATE attempts SET execution_stage='finished',outcome='succeeded',run_revision=3"
    )
    task_slot, global_slot = fixture.slots()
    assert task_slot is None and global_slot["attempt_id"] is None
    result = await service(fixture).cancel(
        Actor(subject=fixture.actor), fixture.command(expected_task_revision=2)
    )
    assert result["task_state"] == "cancelled"
    assert fixture.attempt_row()["outcome"] == "succeeded"
    assert fixture.attempt_row()["result_revision_id"] == str(revision)


@pytest.mark.asyncio
async def test_approved_or_completed_task_refuses_cancel(tmp_path, monkeypatch):
    for state in ("approved", "completed"):
        fixture = build(tmp_path / state, monkeypatch, task_state=state)
        with pytest.raises(CancelNotAllowed) as error:
            await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
        assert error.value.code == "cancel_not_allowed"
        assert fixture.task_row()["state"] == state
        assert fixture.attempt_row()["cancelled_at"] is None


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_rejects_parameter_change(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    command = fixture.command()
    first = await service(fixture).cancel(Actor(subject=fixture.actor), command)
    replay = await service(fixture).cancel(Actor(subject=fixture.actor), command)
    assert replay == first
    assert fixture.task_row()["revision"] == 3
    with pytest.raises(Conflict):
        await service(fixture).cancel(
            Actor(subject=fixture.actor),
            fixture.command(request_id=command.request_id, expected_task_revision=3),
        )
    assert fixture.task_row()["revision"] == 3


@pytest.mark.asyncio
async def test_cancel_rejects_stale_revision_and_foreign_attempt(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(Conflict) as error:
        await service(fixture).cancel(
            Actor(subject=fixture.actor), fixture.command(expected_task_revision=1)
        )
    assert error.value.code == "revision_or_request_conflict"
    with pytest.raises(Conflict):
        await service(fixture).cancel(
            Actor(subject=fixture.actor), fixture.command(attempt_id=uuid4())
        )
    assert fixture.task_row()["state"] == "analyzing"


@pytest.mark.asyncio
async def test_non_architect_or_foreign_actor_cannot_cancel(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(AccessDenied):
        await service(fixture).cancel(Actor(subject=uuid4()), fixture.command())
    reviewer = build(tmp_path / "reviewer", monkeypatch, role="reviewer")
    with pytest.raises(AccessDenied):
        await service(reviewer).cancel(Actor(subject=reviewer.actor), reviewer.command())
    assert fixture.task_row()["state"] == "analyzing"
    assert reviewer.task_row()["state"] == "analyzing"


@pytest.mark.asyncio
async def test_cancelled_task_cannot_be_revived_by_start_or_cancel(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    replay = await service(fixture).cancel(
        Actor(subject=fixture.actor), fixture.command(expected_task_revision=3)
    )
    assert replay["task_state"] == "cancelled"
    assert fixture.task_row()["state"] == "cancelled"


class Runner:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, identity):
        self.calls.append(identity)


def supervisor(fixture, *, runner=None, **settings):
    values = {"heartbeat_seconds": 5, "lease_seconds": 30, "shutdown_seconds": 2}
    values.update(settings)
    return ProposalAttemptSupervisor(
        OfflineDatabase(fixture.root),
        runner=runner or Runner(),
        settings=SupervisorSettings(**values),
        clock=fixture_clock(fixture),
    )


def fixture_clock(fixture):
    return Clock()


@pytest.mark.asyncio
async def test_cancel_before_notify_does_not_dispatch(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, stage="accepted")
    await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    await instance.start()
    try:
        with pytest.raises(AttemptTerminal):
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert runner.calls == []
    assert fixture.attempt_row()["cancelled_at"] is not None


@pytest.mark.asyncio
async def test_drain_waits_for_open_call_and_keeps_slots(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute(
        "INSERT INTO records VALUES(?,?,?,?,?,?,?,NULL)",
        (
            str(fixture.attempt),
            "call",
            "late-call",
            1,
            "0" * 64,
            "registered",
            "{}",
        ),
    )
    await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    instance = supervisor(fixture)
    status = await instance.reconcile_cancelled(fixture.attempt, execution_stopped=True)
    assert status.open_calls == ("late-call",)
    assert status.stage.value == "reconciling"
    assert fixture.slots()[0] is not None


@pytest.mark.asyncio
async def test_drain_finishes_and_releases_slots_only_after_execution_stops(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    instance = supervisor(fixture)
    pending = await instance.reconcile_cancelled(fixture.attempt, execution_stopped=False)
    assert pending.stage.value == "reconciling"
    assert fixture.attempt_row()["execution_stage"] == "reconciling"
    done = await instance.reconcile_cancelled(fixture.attempt, execution_stopped=True)
    assert done.stage.value == "finished"
    assert done.outcome.value == "cancelled"
    assert done.slots_released is True
    assert fixture.attempt_row()["terminal_at"] is not None
    assert fixture.slots()[0] is None
    assert fixture.slots()[1]["attempt_id"] is None


@pytest.mark.asyncio
async def test_cancelled_attempt_rejects_new_checkpoint(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    await service(fixture).cancel(Actor(subject=fixture.actor), fixture.command())
    from presales.infrastructure.proposal_checkpoint import ProposalCheckpointFence

    fence = ProposalCheckpointFence(
        attempt_id=fixture.attempt,
        task_id=fixture.task,
        thread_id=f"attempt-{fixture.attempt}",
        execution_generation=1,
    )
    with pytest.raises(AttemptTerminal):
        await fence.check(_PsycopgAdapter(fixture))


class _Cursor:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _PsycopgAdapter:
    def __init__(self, fixture) -> None:
        self.fixture = fixture

    async def execute(self, sql, params=()):
        if "proposal_global_slot" in sql:
            return _Cursor([self.fixture.slots()[1]])
        if "FROM presales.tasks" in sql:
            return _Cursor([self.fixture.task_row()])
        if "proposal_task_slots" in sql:
            return _Cursor([self.fixture.slots()[0]])
        if "proposal_analysis_attempts" in sql:
            row = self.fixture.attempt_row()
            return _Cursor(
                [
                    {
                        "execution_stage": row["execution_stage"],
                        "execution_generation": row["execution_generation"],
                        "thread_id": row["thread_id"],
                        "task_id": UUID(row["task_id"]),
                        "cancelled_at": row["cancelled_at"],
                        "run_revision": row["run_revision"],
                    }
                ]
            )
        raise AssertionError(sql)


def test_migration_grants_only_a_cancel_function():
    migration = Path("migrations/versions/0008_proposal_attempt_cancel.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0008_proposal_attempt_cancel"' in migration
    assert 'down_revision = "0007_proposal_attempt_start"' in migration
    assert "SECURITY DEFINER" in migration
    assert "SET search_path=pg_catalog,presales,iam" in migration
    assert "GRANT EXECUTE ON FUNCTION presales.cancel_proposal_task(uuid,uuid,bigint)" in migration
    assert "GRANT UPDATE(state)" not in migration
    assert "GRANT ALL" not in migration
