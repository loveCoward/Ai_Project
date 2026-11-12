"""SQLite-backed contracts for Evidence Snapshot closure; PostgreSQL locks are out of scope."""

import json
import re
import sqlite3
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from presales.application.evidence_snapshot import EvidenceSnapshotService
from presales.application.proposal_attempt_context import ProposalAttemptContext
from presales.domain.contracts import AccessDenied, Conflict, digest
from presales.domain.evidence_snapshot import (
    EvidenceContractViolation,
    NodeExecutionIdentity,
    SnapshotCycleDetected,
    SnapshotMissingSource,
    SnapshotNotReady,
    SourceReference,
)
from presales.domain.proposal_analysis_attempt import (
    AttemptIdentity,
    AttemptTerminal,
    FenceConflict,
)

JSON_KEYS = {
    "body",
    "detail",
    "locator",
    "applicability",
    "start_scope",
    "budget",
    "input_manifest",
}
UUID_KEYS = {
    "id",
    "task_id",
    "attempt_id",
    "actor",
    "artifact_id",
    "resource_id",
    "source_version_id",
    "evidence_id",
    "retrieval_run_id",
}
CAST = re.compile(r"CAST\(:(\w+) AS jsonb\)")


class Result:
    def __init__(self, cursor) -> None:
        self.rowcount = cursor.rowcount
        self.values = []
        for row in cursor.fetchall():
            value = dict(row)
            for key, item in value.items():
                if item is not None and key in JSON_KEYS and isinstance(item, str):
                    value[key] = json.loads(item)
                elif item and key in UUID_KEYS and not isinstance(item, UUID):
                    value[key] = UUID(item)
            self.values.append(value)

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.values)

    def first(self):
        return next(iter(self.values), None)

    def scalar_one(self):
        return list(self.values[0].values())[0]


class OfflineDatabase:
    def __init__(self, root) -> None:
        self.root = root

    @asynccontextmanager
    async def transaction(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for schema in ("presales", "iam"):
            conn.execute(f"ATTACH DATABASE ? AS {schema}", (str(self.root / f"{schema}.db"),))

        class Connection:
            async def execute(self, statement, params=None):
                sql = str(statement).replace(" FOR SHARE", "").replace(" FOR UPDATE", "")
                sql = CAST.sub(r":\1", sql).replace("now()", "CURRENT_TIMESTAMP")
                values = {
                    key: str(value) if isinstance(value, UUID) else value
                    for key, value in (params or {}).items()
                }
                return Result(conn.execute(sql, values))

        try:
            yield Connection()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _schema(root) -> None:
    presales = sqlite3.connect(root / "presales.db")
    presales.executescript(
        """
        CREATE TABLE tasks(id TEXT,state TEXT,input_revision INTEGER);
        CREATE TABLE proposal_global_slot(attempt_id TEXT,execution_generation INTEGER);
        CREATE TABLE proposal_task_slots(task_id TEXT,attempt_id TEXT,execution_generation INTEGER);
        CREATE TABLE proposal_analysis_attempts(id TEXT,task_id TEXT,actor TEXT,thread_id TEXT,
            input_revision INTEGER,execution_generation INTEGER,execution_stage TEXT,cancelled_at TEXT,
            start_scope TEXT,budget TEXT,model_mode TEXT,config_identity TEXT,outcome TEXT,
            terminal_at TEXT,run_revision INTEGER,error_code TEXT,updated_at TEXT);
        CREATE TABLE proposal_execution_records(attempt_id TEXT,kind TEXT,record_key TEXT,
            execution_generation INTEGER,input_digest TEXT,status TEXT,body TEXT,result_revision_id TEXT,
            updated_at TEXT,PRIMARY KEY(attempt_id,kind,record_key));
        CREATE TABLE resources(id TEXT,parent_id TEXT,kind TEXT);
        CREATE TABLE resource_versions(id TEXT,resource_id TEXT,ready INTEGER,dependencies_complete INTEGER,
            input_manifest TEXT,source_kind TEXT,declaration TEXT);
        CREATE TABLE direct_dependencies(downstream_id TEXT,upstream_id TEXT);
        CREATE TABLE material_versions(id TEXT);
        CREATE TABLE file_readiness(id TEXT,state TEXT);
        CREATE TABLE knowledge_versions(id TEXT,state TEXT);
        CREATE TABLE evidence_snapshots(artifact_id TEXT PRIMARY KEY,attempt_id TEXT,node_name TEXT,
            input_revision INTEGER,execution_generation INTEGER,reflection_revision INTEGER,
            technical_retry INTEGER,state TEXT,output_digest TEXT,body TEXT,created_at TEXT,
            UNIQUE(attempt_id,node_name,input_revision,reflection_revision,technical_retry));
        CREATE TABLE evidence_snapshot_sources(artifact_id TEXT,ordinal INTEGER,kind TEXT,
            source_version_id TEXT,evidence_id TEXT,locator TEXT,content_sha256 TEXT,
            declaration TEXT,applicability TEXT,retrieval_run_id TEXT,
            PRIMARY KEY(artifact_id,ordinal));
        CREATE TABLE evidence_snapshot_upstream(artifact_id TEXT,ordinal INTEGER,
            upstream_artifact_id TEXT,PRIMARY KEY(artifact_id,ordinal));
        CREATE TABLE node_readiness(attempt_id TEXT,node_key TEXT,execution_generation INTEGER,
            state TEXT,detail TEXT,updated_at TEXT,PRIMARY KEY(attempt_id,node_key));
        """
    )
    presales.commit()
    presales.close()
    iam = sqlite3.connect(root / "iam.db")
    iam.executescript(
        """CREATE TABLE principals(id TEXT,role TEXT,active INTEGER);
        CREATE TABLE authorization_revision(revision INTEGER);
        CREATE TABLE resource_grants(principal_id TEXT,resource_id TEXT);"""
    )
    iam.commit()
    iam.close()


class Harness:
    def __init__(self, root, actor, task, attempt, identity, sources, resources) -> None:
        self.root, self.actor, self.task, self.attempt = root, actor, task, attempt
        self.identity, self.sources, self.resources = identity, sources, resources
        self.service = EvidenceSnapshotService(
            ProposalAttemptContext(OfflineDatabase(root), identity)
        )

    def execute(self, sql, params=()):
        with sqlite3.connect(self.root / "presales.db") as conn:
            conn.execute(sql, params)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    async def no_lock(*args, **kwargs):
        pass

    monkeypatch.setattr("presales.access.policy.lock", no_lock)
    monkeypatch.setattr("presales.application.proposal_attempt_context.lock", no_lock)
    _schema(tmp_path)
    actor, task, attempt = uuid4(), uuid4(), uuid4()
    first, second = uuid4(), uuid4()
    identity = AttemptIdentity(
        attempt_id=attempt,
        task_id=task,
        actor_id=actor,
        thread_id="thread",
        input_revision=1,
        execution_generation=1,
    )
    presales = sqlite3.connect(tmp_path / "presales.db")
    presales.execute("INSERT INTO tasks VALUES(?,'analyzing',1)", (str(task),))
    presales.execute("INSERT INTO proposal_global_slot VALUES(?,1)", (str(attempt),))
    presales.execute("INSERT INTO proposal_task_slots VALUES(?,?,1)", (str(task), str(attempt)))
    presales.execute(
        """INSERT INTO proposal_analysis_attempts
        (id,task_id,actor,thread_id,input_revision,execution_generation,execution_stage,
         cancelled_at,start_scope,budget,model_mode,config_identity,outcome,terminal_at,
         run_revision,error_code,updated_at)
        VALUES(?,?,?,'thread',1,1,'running',NULL,?,'{}','fake','config',NULL,NULL,0,NULL,NULL)""",
        (str(attempt), str(task), str(actor), json.dumps([str(first), str(second)])),
    )
    for identifier in (task, first, second):
        presales.execute(
            "INSERT INTO resources VALUES(?,NULL,?)",
            (str(identifier), "task" if identifier == task else "material"),
        )
    sources = {}
    for index, resource in enumerate((first, second), start=1):
        version = uuid4()
        sources[resource] = version
        presales.execute(
            "INSERT INTO resource_versions VALUES(?,?,1,1,'[]','original','synthetic')",
            (str(version), str(resource)),
        )
        presales.execute(
            "INSERT INTO evidence_snapshots(artifact_id,attempt_id,node_name,input_revision,"
            "execution_generation,reflection_revision,technical_retry,state,output_digest,body)"
            " VALUES(?,?,?,1,1,0,0,'ready',?, '{}')",
            (str(1000 + index), str(attempt), f"root-{index}", "a" * 64),
        )
    presales.commit()
    presales.close()
    iam = sqlite3.connect(tmp_path / "iam.db")
    iam.execute("INSERT INTO principals VALUES(?,'architect',1)", (str(actor),))
    iam.execute("INSERT INTO authorization_revision VALUES(1)")
    iam.executemany(
        "INSERT INTO resource_grants VALUES(?,?)",
        [(str(actor), str(resource)) for resource in (task, first, second)],
    )
    iam.commit()
    iam.close()
    return Harness(tmp_path, actor, task, attempt, identity, sources, (first, second))


def source(harness, resource, *, kind="material", version=None) -> SourceReference:
    return SourceReference(
        kind=kind,
        source_version_id=version or harness.sources[resource],
        locator={"page": 1, "range": "A1:A9"},
        content_sha256="b" * 64,
        declaration="synthetic",
        applicability={"products": ["synthetic-product"]},
        retrieval_run_id=uuid4() if kind == "retrieval" else None,
    )


def node(harness, name="requirements", *, generation=1, retry=0, reflection=0):
    return NodeExecutionIdentity(
        attempt_id=harness.attempt,
        node_name=name,
        input_revision=1,
        execution_generation=generation,
        reflection_revision=reflection,
        technical_retry=retry,
    )


@pytest.mark.asyncio
async def test_each_source_kind_is_registered_with_traceable_closure(harness):
    kinds = ("material", "mcp_background", "retrieval", "reflection")
    sources = tuple(
        source(harness, harness.resources[index % 2], kind=kind) for index, kind in enumerate(kinds)
    )
    artifact = await harness.service.save(node(harness), body={"requirements": []}, sources=sources)
    assert artifact.state == "ready"
    assert [item.kind for item in artifact.sources] == list(kinds)
    assert artifact.sources[0].locator == {"page": 1, "range": "A1:A9"}
    assert artifact.sources[0].content_sha256 == "b" * 64
    assert artifact.sources[0].applicability == {"products": ["synthetic-product"]}
    assert artifact.output_digest == digest(
        {
            "body": {"requirements": []},
            "sources": [item.model_dump(mode="json") for item in sources],
        }
    )
    loaded = await harness.service.load(node(harness))
    assert loaded.artifact_id == artifact.artifact_id
    assert loaded.sources == artifact.sources


@pytest.mark.asyncio
async def test_same_identity_reuses_result_without_second_authority_row(harness):
    identity = node(harness, retry=1)
    first = await harness.service.save(
        identity, body={"requirements": ["r1"]}, sources=(source(harness, harness.resources[0]),)
    )
    second = await harness.service.save(
        identity, body={"requirements": ["r1"]}, sources=(source(harness, harness.resources[0]),)
    )
    assert second.artifact_id == first.artifact_id
    with sqlite3.connect(harness.root / "presales.db") as conn:
        count = conn.execute("SELECT count(*) FROM evidence_snapshots").fetchone()[0]
    assert count == 3


@pytest.mark.asyncio
async def test_same_identity_with_different_output_is_a_conflict(harness):
    identity = node(harness)
    await harness.service.save(
        identity, body={"requirements": ["r1"]}, sources=(source(harness, harness.resources[0]),)
    )
    with pytest.raises(Conflict) as error:
        await harness.service.save(
            identity,
            body={"requirements": ["r2"]},
            sources=(source(harness, harness.resources[0]),),
        )
    assert error.value.code == "revision_or_request_conflict"


@pytest.mark.asyncio
async def test_cycle_and_missing_upstream_are_rejected(harness):
    parent = await harness.service.save(
        node(harness, "parent"),
        body={"requirements": ["r1"]},
        sources=(source(harness, harness.resources[0]),),
    )
    child = await harness.service.save(
        node(harness, "child"),
        body={"requirements": ["r2"]},
        sources=(source(harness, harness.resources[1]),),
        upstream=(parent.artifact_id,),
    )
    with pytest.raises(SnapshotCycleDetected) as cycle:
        await harness.service.link_upstream(
            node(harness, "parent", retry=1),
            artifact_id=parent.artifact_id,
            parent=child.artifact_id,
        )
    assert cycle.value.code == "cycle_detected"
    with pytest.raises(SnapshotMissingSource) as missing:
        await harness.service.save(
            node(harness, "orphan"),
            body={"requirements": ["r4"]},
            sources=(source(harness, harness.resources[0]),),
            upstream=(uuid4(),),
        )
    assert missing.value.code == "missing_source"


@pytest.mark.asyncio
async def test_unregistered_source_and_duplicate_source_are_rejected(harness):
    with pytest.raises(SnapshotMissingSource) as missing:
        await harness.service.save(
            node(harness, "unknown-source"),
            body={},
            sources=(source(harness, harness.resources[0], version=uuid4()),),
        )
    assert missing.value.code == "missing_source"
    duplicated = source(harness, harness.resources[0])
    with pytest.raises(EvidenceContractViolation) as duplicate:
        await harness.service.save(
            node(harness, "duplicate-source"), body={}, sources=(duplicated, duplicated)
        )
    assert duplicate.value.code == "evidence_contract_failed"


@pytest.mark.asyncio
async def test_source_without_current_grant_is_denied_and_not_stored(harness):
    with sqlite3.connect(harness.root / "iam.db") as conn:
        conn.execute(
            "DELETE FROM resource_grants WHERE resource_id=?", (str(harness.resources[1]),)
        )
    with pytest.raises(AccessDenied) as denied:
        await harness.service.save(
            node(harness, "denied"),
            body={},
            sources=(source(harness, harness.resources[1]),),
        )
    assert denied.value.code == "not_found_or_not_allowed"
    with sqlite3.connect(harness.root / "presales.db") as conn:
        stored = conn.execute(
            "SELECT count(*) FROM evidence_snapshots WHERE node_name='denied'"
        ).fetchone()[0]
    assert stored == 0


@pytest.mark.asyncio
async def test_stale_generation_and_terminal_attempt_cannot_write(harness):
    with pytest.raises(FenceConflict) as fenced:
        await harness.service.save(node(harness, "stale", generation=2), body={})
    assert fenced.value.code == "fence_conflict"
    harness.execute("UPDATE proposal_analysis_attempts SET execution_stage='finished'")
    with pytest.raises(AttemptTerminal) as terminal:
        await harness.service.save(node(harness, "late"), body={})
    assert terminal.value.code == "attempt_terminal"


@pytest.mark.asyncio
async def test_reconcile_reuses_committed_artifact(harness):
    identity = node(harness, "evidence")
    saved = await harness.service.save(
        identity, body={"note": "synthetic"}, sources=(source(harness, harness.resources[0]),)
    )
    result = await harness.service.reconcile(identity)
    assert result.state == "reusable"
    assert result.artifact_id == saved.artifact_id
    assert result.open_calls == ()


@pytest.mark.asyncio
async def test_reconcile_keeps_unknown_when_a_call_is_open(harness):
    identity = node(harness, "architecture")
    with sqlite3.connect(harness.root / "presales.db") as conn:
        conn.execute(
            "INSERT INTO proposal_execution_records VALUES(?,?,?,1,?, 'registered', ?, NULL, NULL)",
            (
                str(harness.attempt),
                "call",
                "call-1",
                "c" * 64,
                json.dumps({"node": "architecture"}),
            ),
        )
    result = await harness.service.reconcile(identity)
    assert result.state == "unknown"
    assert result.open_calls == ("call-1",)
    assert result.artifact_id is None


@pytest.mark.asyncio
async def test_reconcile_without_artifact_or_call_is_not_ready(harness):
    result = await harness.service.reconcile(node(harness, "risk"))
    assert result.state == "not_ready"


@pytest.mark.asyncio
async def test_load_missing_snapshot_is_not_ready(harness):
    with pytest.raises(SnapshotNotReady) as error:
        await harness.service.load(node(harness, "missing"))
    assert error.value.code == "not_ready"


@pytest.mark.asyncio
async def test_readiness_ledger_tracks_pending_and_failure(harness):
    identity = node(harness, "coverage")
    await harness.service.mark_pending(identity, detail={"reason": "awaiting upstream"})
    pending = await harness.service.reconcile(identity)
    assert pending.state == "not_ready"
    await harness.service.set_failed(identity, error_code="schema_invalid")
    with sqlite3.connect(harness.root / "presales.db") as conn:
        row = conn.execute(
            "SELECT state,detail FROM node_readiness WHERE node_key=?", (identity.key,)
        ).fetchone()
    assert row[0] == "failed"
    assert json.loads(row[1]) == {"error_code": "schema_invalid"}
