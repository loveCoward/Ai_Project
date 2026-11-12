"""Offline contracts for the start command; PostgreSQL locking and races belong to task 12."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from presales.application.proposal_attempt_commands import (
    BACKGROUND_POLICY_KEY,
    AttemptStartSettings,
    ProposalAttemptStart,
)
from presales.domain.contracts import AccessDenied, Actor, Conflict, InvalidInput, NotReady
from presales.domain.proposal_analysis_attempt import (
    AttemptBusy,
    AttemptTerminal,
    BackgroundRequirement,
    GlobalBusy,
    ModelMode,
    StartAttemptCommand,
)
from presales.infrastructure.config import OutboundServiceSettings
from presales.infrastructure.outbound import OutboundConfigurationError
from presales.infrastructure.proposal_model import ProposalModel

OBJECT_KEYS = {
    "id",
    "actor",
    "task_id",
    "attempt_id",
    "material_version_id",
    "resource_id",
    "result_revision_id",
    "owner",
    "source_version_id",
}
JSON_KEYS = {"body", "budget", "start_scope", "result", "input_manifest"}


class Result:
    def __init__(self, cursor):
        self.rowcount = cursor.rowcount
        self.values = []
        for row in cursor.fetchall():
            value = dict(row)
            for key, item in value.items():
                if item is not None and key in JSON_KEYS:
                    value[key] = json.loads(item)
                elif isinstance(item, UUID):
                    continue
                elif item and key in OBJECT_KEYS:
                    value[key] = UUID(item)
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
    def __init__(self, root):
        self.root = root

    @asynccontextmanager
    async def transaction(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TEMP TABLE ids(value TEXT)")
        for schema in ("presales", "iam", "audit"):
            conn.execute(f"ATTACH DATABASE ? AS {schema}", (str(self.root / f"{schema}.db"),))

        class Connection:
            async def execute(self, statement, params=None):
                params = params or {}
                sql = str(statement).replace(" FOR SHARE", "").replace(" FOR UPDATE", "")
                if "jsonb_array_elements_text(CAST(:ids AS jsonb))" in sql:
                    conn.execute("DELETE FROM temp.ids")
                    conn.executemany(
                        "INSERT INTO temp.ids VALUES(?)",
                        [(item,) for item in json.loads(params["ids"])],
                    )
                    sql = sql.replace(
                        "value::uuid FROM jsonb_array_elements_text(CAST(:ids AS jsonb))",
                        "value FROM (SELECT value FROM temp.ids)",
                    )
                    sql = sql.replace(
                        "jsonb_array_elements_text(CAST(:ids AS jsonb))",
                        "(SELECT value FROM temp.ids)",
                    )
                    params = {key: value for key, value in params.items() if key != "ids"}
                params = {key: _scalar(value) for key, value in params.items()}
                sql = sql.replace("CAST(:body AS jsonb)", ":body")
                sql = sql.replace("CAST(:result AS jsonb)", ":result")
                sql = sql.replace("CAST(:start_scope AS jsonb)", ":start_scope")
                sql = sql.replace("CAST(:budget AS jsonb)", ":budget")
                sql = sql.replace("now()", "CURRENT_TIMESTAMP").replace(
                    "singleton=true", "singleton=1"
                )
                if sql.strip().startswith("SELECT presales.begin_proposal_analysis"):
                    _begin_analysis(conn, UUID(str(params["actor"])), UUID(str(params["task"])))
                    return Result(_empty(conn))
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


def _scalar(value):
    if isinstance(value, UUID | datetime):
        return str(value)
    return value


def _begin_analysis(conn, actor, task):
    principal = conn.execute(
        "SELECT active,role FROM iam.principals WHERE id=?", (str(actor),)
    ).fetchone()
    granted = conn.execute(
        """SELECT 1 FROM presales.resources r
        JOIN iam.resource_grants g ON g.resource_id=r.id
        WHERE r.id=? AND g.principal_id=?""",
        (str(task), str(actor)),
    ).fetchone()
    if not principal or not principal[0] or principal[1] != "architect" or not granted:
        raise sqlite3.IntegrityError("not_found_or_not_allowed")
    cursor = conn.execute(
        """UPDATE tasks SET state='analyzing',revision=revision+1
        WHERE id=? AND state IN ('new','awaiting_supplementation','failed',
        'analyzed_awaiting_submission')""",
        (str(task),),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("revision_or_request_conflict")


def change(root, sql, params=(), schema="presales"):
    with sqlite3.connect(root / f"{schema}.db") as conn:
        conn.execute(sql, params)


class Fixture:
    def __init__(self, root, actor, task, source, materials, versions, global_owner=None):
        self.root, self.actor, self.task, self.source = root, actor, task, source
        self.materials, self.global_owner = materials, global_owner
        self.versions = versions

    def command(self, **overrides):
        options = {
            "request_id": uuid4(),
            "task_id": self.task,
            "expected_task_revision": 1,
            "expected_input_revision": 1,
            "model_mode": ModelMode.FAKE,
            "config_identity": "config",
            "start_scope": (self.source, *self.versions),
            "background_requirement": BackgroundRequirement.REQUIRED,
        }
        options.update(overrides)
        return StartAttemptCommand(**options)


def build(tmp_path, monkeypatch, *, materials=None, global_owner=None):
    async def no_lock(*args, **kwargs):
        pass

    monkeypatch.setattr("presales.access.policy.lock", no_lock)
    monkeypatch.setattr("presales.application.proposal_attempt_commands.lock", no_lock)

    actor, task, source = uuid4(), uuid4(), uuid4()
    materials = materials if materials is not None else [("m1", "required", "ready", 100)]
    presales = sqlite3.connect(tmp_path / "presales.db")
    presales.executescript(
        """
        CREATE TABLE tasks(id TEXT,state TEXT,revision INTEGER,input_revision INTEGER);
        CREATE TABLE resources(id TEXT,parent_id TEXT,kind TEXT);
        CREATE TABLE resource_versions(id TEXT,resource_id TEXT,ready INTEGER,
            dependencies_complete INTEGER,input_manifest TEXT,source_kind TEXT,declaration TEXT);
        CREATE TABLE direct_dependencies(downstream_id TEXT,upstream_id TEXT);
        CREATE TABLE material_versions(id TEXT);
        CREATE TABLE file_readiness(id TEXT,state TEXT);
        CREATE TABLE task_inputs(task_id TEXT,material_version_id TEXT);
        CREATE TABLE task_input_manifest(task_id TEXT,input_revision INTEGER,
            material_version_id TEXT,necessity TEXT);
        CREATE TABLE parse_jobs(id TEXT,source_version_id TEXT,parser_version TEXT,state TEXT);
        CREATE TABLE parse_results(id TEXT,job_id TEXT,character_count INTEGER);
        CREATE TABLE proposal_analysis_attempts(id TEXT PRIMARY KEY,task_id TEXT,actor TEXT,
            thread_id TEXT UNIQUE,input_revision INTEGER,start_scope TEXT,model_mode TEXT,
            config_identity TEXT,execution_generation INTEGER,execution_stage TEXT,outcome TEXT,
            run_revision INTEGER,result_ready INTEGER DEFAULT 0,result_revision_id TEXT,
            candidate_version_id TEXT,source_snapshot TEXT,cancelled_at TEXT,terminal_at TEXT,
            error_code TEXT,budget TEXT,reflection_revision INTEGER DEFAULT 0,
            updated_at TEXT,created_at TEXT);
        CREATE TABLE proposal_execution_records(attempt_id TEXT,kind TEXT,record_key TEXT,
            execution_generation INTEGER,input_digest TEXT,status TEXT,body TEXT,
            result_revision_id TEXT,PRIMARY KEY(attempt_id,kind,record_key));
        CREATE TABLE proposal_task_slots(task_id TEXT PRIMARY KEY,attempt_id TEXT,
            execution_generation INTEGER,owner TEXT,lease_until TEXT);
        CREATE TABLE proposal_global_slot(singleton INTEGER PRIMARY KEY,attempt_id TEXT,
            execution_generation INTEGER,owner TEXT,lease_until TEXT);
        CREATE TABLE command_receipts(actor TEXT,operation TEXT,request_id TEXT,
            parameter_digest TEXT,result TEXT,PRIMARY KEY(actor,operation,request_id));
        """
    )
    presales.execute("INSERT INTO tasks VALUES(?,?,1,1)", (str(task), "new"))
    presales.execute("INSERT INTO resources VALUES(?,NULL,'task')", (str(task),))
    presales.execute("INSERT INTO resources VALUES(?,NULL,'knowledge')", (str(source),))
    presales.execute(
        "INSERT INTO resource_versions VALUES(?,?,1,1,'[]','original','synthetic')",
        (str(source), str(source)),
    )
    presales.execute("INSERT INTO proposal_global_slot VALUES(1,NULL,NULL,NULL,NULL)")
    versions = []
    for name, necessity, state, characters in materials:
        version = uuid4()
        versions.append(version)
        presales.execute("INSERT INTO task_inputs VALUES(?,?)", (str(task), str(version)))
        presales.execute(
            "INSERT INTO task_input_manifest VALUES(?,1,?,?)", (str(task), str(version), necessity)
        )
        presales.execute(
            "INSERT INTO resource_versions VALUES(?,?,1,1,'[]','original','synthetic')",
            (str(version), str(version)),
        )
        presales.execute("INSERT INTO resources VALUES(?,?,'material')", (str(version), str(task)))
        presales.execute("INSERT INTO material_versions VALUES(?)", (str(version),))
        presales.execute("INSERT INTO file_readiness VALUES(?,?)", (str(version), state))
        if state == "ready":
            presales.execute(
                "INSERT INTO parse_jobs VALUES(?,?,'located-1.0','succeeded')",
                (str(uuid4()), str(version)),
            )
            presales.execute(
                "INSERT INTO parse_results VALUES(?,?,?)",
                (
                    str(uuid4()),
                    presales.execute("SELECT id FROM parse_jobs").fetchone()[0],
                    characters,
                ),
            )
    if global_owner is not None:
        presales.execute(
            "UPDATE proposal_global_slot SET attempt_id=?,execution_generation=1,owner=?,"
            "lease_until='2099-01-01T00:00:00+00:00'",
            (str(uuid4()), str(global_owner)),
        )
    presales.commit()
    presales.close()
    iam = sqlite3.connect(tmp_path / "iam.db")
    iam.executescript(
        """CREATE TABLE principals(id TEXT,role TEXT,active INTEGER);
        CREATE TABLE authorization_revision(revision INTEGER);
        CREATE TABLE resource_grants(principal_id TEXT,resource_id TEXT);"""
    )
    iam.execute("INSERT INTO principals VALUES(?,'architect',1)", (str(actor),))
    iam.execute("INSERT INTO authorization_revision VALUES(1)")
    iam.executemany(
        "INSERT INTO resource_grants VALUES(?,?)",
        [
            (str(actor), str(task)),
            (str(actor), str(source)),
            *((str(actor), str(version)) for version in versions),
        ],
    )
    iam.commit()
    iam.close()
    audit = sqlite3.connect(tmp_path / "audit.db")
    audit.executescript(
        """CREATE TABLE events(id TEXT,actor TEXT,operation TEXT,target TEXT,revision INTEGER,
        result TEXT,request_id TEXT,authorization_revision INTEGER);"""
    )
    audit.commit()
    audit.close()
    fixture = Fixture(tmp_path, actor, task, source, materials, tuple(versions))
    return fixture


def model(mode="fake", fake=lambda _: "synthetic answer", **extra):
    options = {"mode": mode, "allowed_data_classes": {"synthetic"}}
    if mode == "real":
        options.update(
            endpoint="http://127.0.0.1/v1/chat/completions",
            destination_alias="local",
            service_identity="model",
        )
    settings = OutboundServiceSettings(**(options | extra))
    return ProposalModel(settings, "config", fake=fake)


def starter(fixture, *, client=None, **settings):
    return ProposalAttemptStart(
        OfflineDatabase(fixture.root),
        model=model() if client is None else client,
        settings=AttemptStartSettings(**settings),
    )


@pytest.mark.asyncio
async def test_legal_command_accepts_attempt_slots_state_receipt_and_audit(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    command = fixture.command()
    result = await starter(fixture).start(Actor(subject=fixture.actor), command)
    assert result["stage"] == "accepted" and result["run_revision"] == 1
    assert result["thread_id"] == f"attempt-{result['attempt_id']}"
    with sqlite3.connect(fixture.root / "presales.db") as conn:
        assert conn.execute("SELECT state,revision FROM tasks").fetchone() == ("analyzing", 2)
        attempt = conn.execute(
            "SELECT execution_stage,run_revision,execution_generation,input_revision,model_mode,"
            "config_identity FROM proposal_analysis_attempts"
        ).fetchone()
        assert attempt == ("accepted", 1, 1, 1, "fake", "config")
        assert (
            conn.execute("SELECT attempt_id FROM proposal_task_slots").fetchone()[0]
            == (result["attempt_id"])
        )
        assert (
            conn.execute("SELECT attempt_id FROM proposal_global_slot").fetchone()[0]
            == (result["attempt_id"])
        )
        assert conn.execute(
            "SELECT record_key,status FROM proposal_execution_records"
        ).fetchone() == (
            BACKGROUND_POLICY_KEY,
            "completed",
        )
        receipt = conn.execute("SELECT operation,result FROM command_receipts").fetchone()
        assert receipt[0] == "start_proposal_attempt"
        assert json.loads(receipt[1])["attempt_id"] == result["attempt_id"]
    with sqlite3.connect(fixture.root / "audit.db") as conn:
        assert conn.execute("SELECT operation,result FROM events").fetchone() == (
            "start_proposal_attempt",
            "accepted",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["new", "awaiting_supplementation", "failed", "analyzed_awaiting_submission"]
)
async def test_startable_task_states_are_accepted(tmp_path, monkeypatch, state):
    fixture = build(tmp_path, monkeypatch)
    change(fixture.root, "UPDATE tasks SET state=?", (state,))
    result = await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert result["stage"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["analyzing", "approved"])
async def test_non_startable_states_are_rejected_with_conflict(tmp_path, monkeypatch, state):
    fixture = build(tmp_path, monkeypatch)
    change(fixture.root, "UPDATE tasks SET state=?", (state,))
    with pytest.raises(Conflict) as error:
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert error.value.code == "revision_or_request_conflict"
    assert _state(fixture) == state
    assert _attempts(fixture) == 0


@pytest.mark.asyncio
async def test_cancelled_task_cannot_be_started_and_stays_cancelled(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    change(fixture.root, "UPDATE tasks SET state='cancelled'")
    with pytest.raises(AttemptTerminal) as error:
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert error.value.code == "attempt_terminal"
    assert _state(fixture) == "cancelled" and _attempts(fixture) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("revision", 2), ("input_revision", 2)],
)
async def test_stale_task_or_input_revision_is_rejected(tmp_path, monkeypatch, field, value):
    fixture = build(tmp_path, monkeypatch)
    change(fixture.root, f"UPDATE tasks SET {field}=?", (value,))
    with pytest.raises(Conflict) as error:
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert error.value.code == "revision_or_request_conflict"
    assert _attempts(fixture) == 0 and _state(fixture) == "new"


@pytest.mark.asyncio
async def test_global_busy_rejects_without_queueing_or_writing(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, global_owner=uuid4())
    with pytest.raises(GlobalBusy) as error:
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert error.value.code == "global_busy"
    assert _attempts(fixture) == 0 and _state(fixture) == "new"
    with sqlite3.connect(fixture.root / "presales.db") as conn:
        assert conn.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_second_attempt_on_same_task_is_attempt_busy(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    service = starter(fixture)
    await service.start(Actor(subject=fixture.actor), fixture.command())
    with pytest.raises(AttemptBusy) as error:
        await service.start(Actor(subject=fixture.actor), fixture.command())
    assert error.value.code == "attempt_busy"
    assert _attempts(fixture) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "materials,necessity",
    [
        ([("m1", "required", "pending", 0)], "required"),
        ([("m1", "unclassified", "ready", 10)], "unclassified"),
    ],
)
async def test_unready_or_unclassified_key_input_is_not_ready(
    tmp_path, monkeypatch, materials, necessity
):
    fixture = build(tmp_path, monkeypatch, materials=materials)
    with pytest.raises(NotReady) as error:
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert error.value.code == "not_ready"
    assert _attempts(fixture) == 0 and _state(fixture) == "new"


@pytest.mark.asyncio
async def test_optional_input_is_reported_missing_but_does_not_block(tmp_path, monkeypatch):
    fixture = build(
        tmp_path,
        monkeypatch,
        materials=[("m1", "required", "ready", 10), ("m2", "optional", "pending", 0)],
    )
    result = await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert len(result["missing_optional_sources"]) == 1
    assert _attempts(fixture) == 1


@pytest.mark.asyncio
async def test_key_input_over_capacity_is_rejected_without_truncation(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, materials=[("m1", "required", "ready", 500)])
    with pytest.raises(InvalidInput) as error:
        await starter(fixture, max_key_input_characters=100).start(
            Actor(subject=fixture.actor), fixture.command()
        )
    assert error.value.code == "invalid_input"
    assert _attempts(fixture) == 0 and _state(fixture) == "new"


@pytest.mark.asyncio
async def test_key_input_at_capacity_boundary_is_accepted(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, materials=[("m1", "required", "ready", 100)])
    result = await starter(fixture, max_key_input_characters=100).start(
        Actor(subject=fixture.actor), fixture.command()
    )
    assert result["stage"] == "accepted"


@pytest.mark.asyncio
async def test_unconfirmed_background_is_refused_by_contract(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        fixture.command(background_requirement=BackgroundRequirement.UNCONFIRMED)
    assert _attempts(fixture) == 0


@pytest.mark.asyncio
async def test_unselected_model_mode_or_missing_client_refuses_to_start(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    disabled = ProposalModel(
        OutboundServiceSettings(mode="disabled", allowed_data_classes={"synthetic"}), "config"
    )
    with pytest.raises(OutboundConfigurationError):
        await starter(fixture, client=disabled).start(
            Actor(subject=fixture.actor), fixture.command()
        )
    with pytest.raises(InvalidInput):
        await ProposalAttemptStart(OfflineDatabase(fixture.root)).start(
            Actor(subject=fixture.actor), fixture.command()
        )
    assert _attempts(fixture) == 0 and _state(fixture) == "new"


@pytest.mark.asyncio
async def test_configuration_identity_mismatch_is_refused(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(OutboundConfigurationError):
        await starter(fixture).start(
            Actor(subject=fixture.actor), fixture.command(config_identity="other")
        )
    assert _attempts(fixture) == 0


@pytest.mark.asyncio
async def test_input_outside_declared_start_scope_is_denied(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    outside = uuid4()
    with pytest.raises(AccessDenied) as error:
        await starter(fixture).start(
            Actor(subject=fixture.actor), fixture.command(start_scope=(outside,))
        )
    assert error.value.code == "not_found_or_not_allowed"
    assert _attempts(fixture) == 0 and _state(fixture) == "new"


@pytest.mark.asyncio
async def test_replay_returns_same_attempt_and_conflicting_parameters_are_rejected(
    tmp_path, monkeypatch
):
    fixture = build(tmp_path, monkeypatch)
    service = starter(fixture)
    command = fixture.command()
    first = await service.start(Actor(subject=fixture.actor), command)
    replay = await service.start(Actor(subject=fixture.actor), command)
    assert replay == first
    assert _attempts(fixture) == 1
    with pytest.raises(Conflict) as error:
        await service.start(
            Actor(subject=fixture.actor),
            fixture.command(request_id=command.request_id, config_identity="other"),
        )
    assert error.value.code == "revision_or_request_conflict"
    assert _attempts(fixture) == 1


@pytest.mark.asyncio
async def test_non_architect_and_revoked_actor_cannot_start(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    with pytest.raises(AccessDenied):
        await starter(fixture).start(Actor(subject=uuid4()), fixture.command())
    change(fixture.root, "UPDATE principals SET active=0", schema="iam")
    with pytest.raises(AccessDenied):
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert _attempts(fixture) == 0 and _state(fixture) == "new"


@pytest.mark.asyncio
async def test_maintainer_role_cannot_start(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    change(fixture.root, "UPDATE principals SET role='reviewer'", schema="iam")
    with pytest.raises(AccessDenied):
        await starter(fixture).start(Actor(subject=fixture.actor), fixture.command())
    assert _attempts(fixture) == 0


def test_migration_grants_only_a_state_transition_function():
    migration = Path("migrations/versions/0007_proposal_attempt_start.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0007_proposal_attempt_start"' in migration
    assert 'down_revision = "0006_proposal_attempt"' in migration
    assert "SECURITY DEFINER" in migration
    assert "SET search_path=pg_catalog,presales,iam" in migration
    assert "pg_advisory_xact_lock_shared(72401,1)" in migration
    assert "state='analyzing'" in migration
    assert "('new','awaiting_supplementation','failed'," in migration
    assert "'analyzed_awaiting_submission')" in migration
    assert "GET DIAGNOSTICS changed = ROW_COUNT" in migration
    assert "GRANT EXECUTE ON FUNCTION presales.begin_proposal_analysis(uuid,uuid)" in migration
    assert (
        "REVOKE ALL ON FUNCTION presales.begin_proposal_analysis(uuid,uuid) FROM PUBLIC"
        in migration
    )
    assert "GRANT UPDATE(state)" not in migration
    assert "GRANT ALL" not in migration


def _state(fixture):
    with sqlite3.connect(fixture.root / "presales.db") as conn:
        return conn.execute("SELECT state FROM tasks").fetchone()[0]


def _attempts(fixture):
    with sqlite3.connect(fixture.root / "presales.db") as conn:
        return conn.execute("SELECT count(*) FROM proposal_analysis_attempts").fetchone()[0]
