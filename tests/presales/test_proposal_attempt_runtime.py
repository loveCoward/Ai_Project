"""Offline contracts for the Attempt supervisor; real process races belong to task 12."""

import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from presales.access.jwt import JwtVerifier
from presales.api.app import create_app
from presales.api.auth import create_auth_app
from presales.application.auth import AuthenticationService
from presales.application.proposal_attempt_runtime import (
    AnalysisRuntimeUnavailable,
    ProposalAttemptSupervisor,
    SupervisorSettings,
    SupervisorStopped,
    supervisor_lifecycle,
)
from presales.domain.contracts import (
    DomainError,
    NotReady,
    PrincipalStatus,
    Role,
    StorageUnavailable,
)
from presales.domain.proposal_analysis_attempt import (
    AttemptBusy,
    AttemptTerminal,
    FenceConflict,
)
from presales.infrastructure.config import JwtSettings

TABLES = {
    "presales.proposal_analysis_attempts": "attempts",
    "presales.proposal_task_slots": "task_slots",
    "presales.proposal_global_slot": "global_slot",
    "presales.proposal_execution_records": "records",
}
JSON_COLUMNS = {"body", "start_scope", "budget"}
UUID_COLUMNS = {"attempt_id", "task_id", "actor"}
TIME_COLUMNS = {"lease_until", "cancelled_at", "terminal_at", "created_at", "updated_at"}


class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class Result:
    def __init__(self, cursor) -> None:
        self.rowcount = cursor.rowcount
        self.values = []
        for row in cursor.fetchall():
            value = dict(row)
            for key, item in value.items():
                if item is None:
                    continue
                if key in JSON_COLUMNS:
                    value[key] = json.loads(item)
                elif key in UUID_COLUMNS:
                    value[key] = UUID(item)
                elif key in TIME_COLUMNS:
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


def _scalars(params):
    values = {}
    for key, value in params.items():
        if isinstance(value, dict | list | tuple):
            values[key] = json.dumps(value, sort_keys=True)
        elif isinstance(value, UUID | datetime):
            values[key] = str(value)
        else:
            values[key] = value
    return values


class OfflineDatabase:
    def __init__(self, root: Path, *, failing: bool = False) -> None:
        self.root, self.failing = Path(root), failing

    @asynccontextmanager
    async def transaction(self):
        if self.failing:
            raise StorageUnavailable()
        conn = sqlite3.connect(self.root / "presales.db")
        conn.row_factory = sqlite3.Row

        class Connection:
            async def execute(self, statement, params=None):
                sql = str(statement)
                for qualified, table in TABLES.items():
                    sql = sql.replace(qualified, table)
                sql = sql.replace(" FOR UPDATE", "").replace(" FOR SHARE", "")
                sql = sql.replace("singleton=true", "singleton=1")
                sql = sql.replace("now()", "CURRENT_TIMESTAMP")
                sql = sql.replace("CAST(:body AS jsonb)", ":body")
                return Result(conn.execute(sql, _scalars(params or {})))

        try:
            yield Connection()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class Fixture:
    def __init__(self, root, task, attempt, clock) -> None:
        self.root, self.task, self.attempt, self.clock = Path(root), task, attempt, clock

    def connect(self):
        conn = sqlite3.connect(self.root / "presales.db")
        conn.row_factory = sqlite3.Row
        return conn

    def attempt_row(self):
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM attempts").fetchone())

    def records(self, kind=None):
        with self.connect() as conn:
            values = [dict(row) for row in conn.execute("SELECT * FROM records").fetchall()]
        return [row for row in values if kind is None or row["kind"] == kind]

    def leases(self):
        with self.connect() as conn:
            task_lease = conn.execute("SELECT lease_until FROM task_slots").fetchone()
            global_lease = conn.execute("SELECT lease_until FROM global_slot").fetchone()
        return (task_lease[0] if task_lease else None, global_lease[0] if global_lease else None)

    def execute(self, sql, params=()):
        with self.connect() as conn:
            conn.execute(sql, params)


def build(tmp_path, *, stage="accepted", lease_seconds=30, clock=None):
    clock = clock or Clock()
    actor, task, attempt = uuid4(), uuid4(), uuid4()
    lease = (clock() + timedelta(seconds=lease_seconds)).isoformat()
    presales = sqlite3.connect(tmp_path / "presales.db")
    presales.executescript(
        """
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
        """
    )
    presales.execute(
        "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,0,NULL,NULL,NULL,NULL,NULL,"
        "NULL,?,0,'2026-11-11T09:00:00+00:00','2026-11-11T09:00:00+00:00')",
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
            1,
            json.dumps({"max_active_seconds": 600}),
        ),
    )
    if stage == "finished":
        presales.execute("INSERT INTO global_slot VALUES(1,NULL,NULL,NULL,NULL)")
    else:
        presales.execute(
            "INSERT INTO task_slots VALUES(?,?,1,?,?)",
            (str(task), str(attempt), str(actor), lease),
        )
        presales.execute(
            "INSERT INTO global_slot VALUES(1,?,1,?,?)", (str(attempt), str(actor), lease)
        )
    presales.commit()
    presales.close()
    return Fixture(tmp_path, task, attempt, clock)


class Runner:
    def __init__(self, outcome=None, calls=None) -> None:
        self.outcome = outcome
        self.calls = calls if calls is not None else []

    async def run(self, identity):
        self.calls.append(identity)
        if self.outcome is not None:
            raise self.outcome


def supervisor(fixture, *, runner=None, failing=False, **settings):
    options = {"heartbeat_seconds": 5, "lease_seconds": 30, "shutdown_seconds": 2}
    options.update(settings)
    return ProposalAttemptSupervisor(
        OfflineDatabase(fixture.root, failing=failing),
        runner=runner,
        settings=SupervisorSettings(**options),
        clock=fixture.clock,
    )


@pytest.mark.asyncio
async def test_claim_moves_accepted_attempt_to_running_and_keeps_slots(tmp_path):
    fixture = build(tmp_path)
    instance = supervisor(fixture)
    await instance.start()
    try:
        claim = await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert claim.identity.execution_generation == 1
    assert claim.identity.thread_id == f"attempt-{fixture.attempt}"
    assert claim.run_revision == 2
    row = fixture.attempt_row()
    assert (row["execution_stage"], row["run_revision"]) == ("running", 2)
    assert all(lease is not None for lease in fixture.leases())


@pytest.mark.asyncio
async def test_second_claim_of_same_attempt_is_busy_and_does_not_redispatch(tmp_path):
    fixture = build(tmp_path)
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    await instance.start()
    try:
        await instance.claim(fixture.attempt)
        with pytest.raises(AttemptBusy) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "attempt_busy"
    assert fixture.attempt_row()["run_revision"] == 2
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,expected",
    [("running", AttemptBusy), ("waiting_human", NotReady)],
)
async def test_claim_outside_accepted_is_refused_without_transition(tmp_path, stage, expected):
    fixture = build(tmp_path, stage=stage)
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(expected) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == expected.code
    assert fixture.attempt_row()["execution_stage"] == stage


@pytest.mark.asyncio
async def test_reconciling_attempt_without_recorded_intent_is_not_claimed(tmp_path):
    fixture = build(tmp_path, stage="reconciling")
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(NotReady) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "not_ready"
    row = fixture.attempt_row()
    assert (row["execution_stage"], row["execution_generation"]) == ("reconciling", 1)


@pytest.mark.asyncio
async def test_terminal_attempt_cannot_be_claimed_or_dispatched(tmp_path):
    fixture = build(tmp_path, stage="finished")
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    await instance.start()
    try:
        with pytest.raises(AttemptTerminal) as error:
            await instance.dispatch(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "attempt_terminal"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_missing_task_slot_fences_heartbeat(tmp_path):
    fixture = build(tmp_path, stage="running")
    fixture.execute("DELETE FROM task_slots")
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(FenceConflict) as error:
            await instance.heartbeat(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "fence_conflict"


@pytest.mark.asyncio
async def test_expired_lease_blocks_claim_and_reports_without_takeover(tmp_path):
    clock = Clock()
    fixture = build(tmp_path, lease_seconds=30, clock=clock)
    clock.advance(31)
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    await instance.start()
    try:
        with pytest.raises(NotReady) as error:
            await instance.claim(fixture.attempt)
        findings = await instance.scan()
    finally:
        await instance.stop()
    assert error.value.code == "not_ready"
    assert runner.calls == []
    assert [finding.lease_expired for finding in findings] == [True]
    assert fixture.attempt_row()["execution_stage"] == "accepted"


@pytest.mark.asyncio
async def test_heartbeat_refreshes_both_leases(tmp_path):
    clock = Clock()
    fixture = build(tmp_path, stage="running", lease_seconds=30, clock=clock)
    instance = supervisor(fixture)
    await instance.start()
    try:
        clock.advance(10)
        lease = await instance.heartbeat(fixture.attempt)
    finally:
        await instance.stop()
    assert lease == clock() + timedelta(seconds=30)
    task_lease, global_lease = (datetime.fromisoformat(item) for item in fixture.leases())
    assert task_lease == global_lease == lease


@pytest.mark.asyncio
async def test_heartbeat_after_cancellation_is_terminal(tmp_path):
    fixture = build(tmp_path, stage="running")
    fixture.execute(
        "UPDATE attempts SET cancelled_at=? WHERE id=?",
        ("2026-11-11T09:00:30+00:00", str(fixture.attempt)),
    )
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(AttemptTerminal) as error:
            await instance.heartbeat(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "attempt_terminal"


@pytest.mark.asyncio
async def test_dispatch_without_injected_runner_refuses_to_run_a_graph(tmp_path):
    fixture = build(tmp_path)
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(AnalysisRuntimeUnavailable) as error:
            await instance.dispatch(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "analysis_runtime_unavailable"
    assert fixture.attempt_row()["execution_stage"] == "accepted"
    assert fixture.leases()[0] is not None


def test_runtime_module_never_imports_the_generic_research_graph():
    source = Path("src/presales/application/proposal_attempt_runtime.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("from agents", "import agents", "service.service", "get_agent", "astream"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_notify_dispatches_injected_port_and_outlives_the_caller(tmp_path):
    fixture = build(tmp_path)
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    async with supervisor_lifecycle(instance):
        await instance.notify(fixture.attempt)
        while not runner.calls:
            await asyncio.sleep(0.01)
        assert instance.accepting is True
        assert fixture.attempt_row()["cancelled_at"] is None
    await instance.wait_idle()
    assert [identity.attempt_id for identity in runner.calls] == [fixture.attempt]
    assert fixture.attempt_row()["execution_stage"] == "running"
    assert fixture.attempt_row()["cancelled_at"] is None


@pytest.mark.asyncio
async def test_notify_after_shutdown_is_refused(tmp_path):
    fixture = build(tmp_path)
    instance = supervisor(fixture, runner=Runner())
    await instance.start()
    await instance.stop()
    with pytest.raises(SupervisorStopped) as error:
        await instance.notify(fixture.attempt)
    assert error.value.code == "supervisor_stopped"
    assert fixture.attempt_row()["execution_stage"] == "accepted"


@pytest.mark.asyncio
async def test_scan_records_accepted_not_dispatched_without_dispatching(tmp_path):
    fixture = build(tmp_path)
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    findings = await instance.scan()
    assert [(f.code, f.stage, f.run_revision) for f in findings] == [
        ("accepted_not_dispatched", "accepted", 1)
    ]
    assert runner.calls == []
    assert fixture.attempt_row()["execution_stage"] == "accepted"
    body = json.loads(fixture.records(kind="recovery")[0]["body"])
    assert body["type"] == "supervisor_scan"
    assert body["code"] == "accepted_not_dispatched"


@pytest.mark.asyncio
async def test_scan_marks_unconfirmed_running_attempt_without_calling_the_runner(tmp_path):
    clock = Clock()
    fixture = build(tmp_path, stage="running", lease_seconds=30, clock=clock)
    clock.advance(31)
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    findings = await instance.scan()
    row = fixture.attempt_row()
    assert [(f.code, f.lease_expired) for f in findings] == [("needs_reconciliation", True)]
    assert row["execution_stage"] == "reconciling"
    assert row["error_code"] == "restart_unconfirmed"
    assert row["outcome"] is None
    assert runner.calls == []


@pytest.mark.asyncio
async def test_scan_does_not_reopen_reconciling_attempt(tmp_path):
    fixture = build(tmp_path, stage="reconciling")
    runner = Runner()
    instance = supervisor(fixture, runner=runner)
    findings = await instance.scan()
    assert [f.code for f in findings] == ["awaiting_reconciliation"]
    assert fixture.attempt_row()["execution_stage"] == "reconciling"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_storage_loss_halts_supervisor_and_blocks_new_calls(tmp_path):
    fixture = build(tmp_path)
    runner = Runner()
    instance = supervisor(fixture, runner=runner, failing=True)
    await instance.start()
    assert instance.halted is True
    assert instance.accepting is False
    with pytest.raises(StorageUnavailable):
        await instance.notify(fixture.attempt)
    with pytest.raises(StorageUnavailable):
        await instance.claim(fixture.attempt)
    with pytest.raises(StorageUnavailable):
        await instance.scan()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_runner_failure_settles_unknown_and_keeps_slots(tmp_path):
    fixture = build(tmp_path)
    instance = supervisor(fixture, runner=Runner(outcome=DomainError()))
    await instance.start()
    try:
        with pytest.raises(DomainError):
            await instance.dispatch(fixture.attempt)
    finally:
        await instance.stop()
    row = fixture.attempt_row()
    assert row["execution_stage"] == "reconciling"
    assert row["outcome"] is None
    assert all(lease is not None for lease in fixture.leases())


@pytest.mark.asyncio
async def test_runner_without_error_keeps_running_instead_of_cancelling(tmp_path):
    fixture = build(tmp_path)
    instance = supervisor(fixture, runner=Runner())
    await instance.start()
    try:
        await instance.dispatch(fixture.attempt)
    finally:
        await instance.stop()
    row = fixture.attempt_row()
    assert row["execution_stage"] == "running"
    assert row["cancelled_at"] is None


@pytest.mark.asyncio
async def test_shutdown_timeout_preserves_unknown_state_and_slots(tmp_path):
    started = asyncio.Event()

    class Slow:
        async def run(self, identity):
            started.set()
            await asyncio.sleep(30)

    fixture = build(tmp_path)
    instance = supervisor(fixture, runner=Slow(), shutdown_seconds=0.05)
    await instance.start()
    await instance.notify(fixture.attempt)
    await asyncio.wait_for(started.wait(), timeout=2)
    await instance.stop()
    row = fixture.attempt_row()
    assert row["execution_stage"] == "reconciling"
    assert row["outcome"] is None
    assert row["error_code"] == "shutdown_unconfirmed"
    assert all(lease is not None for lease in fixture.leases())


def test_settings_require_heartbeat_well_inside_lease():
    with pytest.raises(ValueError):
        SupervisorSettings(heartbeat_seconds=30, lease_seconds=30)
    assert SupervisorSettings(heartbeat_seconds=5, lease_seconds=30).lease_seconds == 30


class PrincipalStore:
    def __init__(self, subject) -> None:
        self.principal = PrincipalStatus(subject=subject, role=Role.ARCHITECT, active=True)

    async def find(self, subject):
        return self.principal if subject == self.principal.subject else None


def jwt_settings():
    return JwtSettings(
        secret=SecretStr("runtime-signing-key-long-enough-for-tests"),
        issuer="presales-runtime",
        audience="presales-browser",
    )


def test_auth_app_enters_and_exits_its_background_lifecycle():
    events = []

    @asynccontextmanager
    async def background():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    settings = jwt_settings()
    app = create_auth_app(
        AuthenticationService(PrincipalStore(uuid4()), settings, ()),
        JwtVerifier(settings),
        background=background,
    )
    with TestClient(app) as client:
        assert events == ["enter"]
        assert client.get("/auth/me").status_code == 401
    assert events == ["enter", "exit"]


def test_create_app_assembles_supervisor_into_the_presales_lifespan(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("PRESALES_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRESALES_DATABASE_URL", "postgresql+psycopg://local@localhost/test")
    monkeypatch.setenv("PRESALES_FILE_ROOT", str(tmp_path / "files"))
    monkeypatch.setenv("PRESALES_JWT_SECRET", "runtime-signing-key-long-enough-for-tests")
    monkeypatch.setenv("PRESALES_JWT_ISSUER", "presales-runtime")
    monkeypatch.setenv("PRESALES_JWT_AUDIENCE", "presales-browser")
    for role in ("SALES", "ARCHITECT", "REVIEWER"):
        monkeypatch.setenv(f"PRESALES_DEMO_{role}_USERNAME", role.lower())
        monkeypatch.setenv(f"PRESALES_DEMO_{role}_PASSWORD", "synthetic-local-password")

    app = create_app()
    assert isinstance(app.state.supervisor, ProposalAttemptSupervisor)
    assert app.state.supervisor.runner is None
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401
        assert app.state.supervisor.halted is True
        assert app.state.supervisor.accepting is False
    assert app.state.supervisor.accepting is False


@pytest.mark.asyncio
async def test_claim_with_foreign_global_slot_is_fenced(tmp_path):
    fixture = build(tmp_path)
    fixture.execute(
        "UPDATE global_slot SET attempt_id=?,execution_generation=1",
        (str(uuid4()),),
    )
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(FenceConflict) as error:
            await instance.claim(fixture.attempt)
    finally:
        await instance.stop()
    assert error.value.code == "fence_conflict"
    assert fixture.attempt_row()["execution_stage"] == "accepted"


@pytest.mark.asyncio
async def test_cancelled_attempt_is_terminal_for_claim_and_heartbeat(tmp_path):
    fixture = build(tmp_path, stage="running")
    fixture.execute("UPDATE attempts SET cancelled_at=?", ("2026-11-11T09:00:30+00:00",))
    instance = supervisor(fixture)
    await instance.start()
    try:
        with pytest.raises(AttemptTerminal) as claim_error:
            await instance.claim(fixture.attempt)
        with pytest.raises(AttemptTerminal) as heartbeat_error:
            await instance.heartbeat(fixture.attempt)
    finally:
        await instance.stop()
    assert claim_error.value.code == "attempt_terminal"
    assert heartbeat_error.value.code == "attempt_terminal"
    assert fixture.attempt_row()["execution_stage"] == "running"
