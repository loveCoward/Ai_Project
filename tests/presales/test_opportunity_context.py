"""Offline contracts for read-only opportunity background; stdio round-trips belong to task 14."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from presales.application.opportunity_context import (
    MAX_CONTEXT_CHARACTERS,
    MAX_CONTEXT_RECORDS,
    READ_CONTEXT_RETRIES,
    ContextCapacityExceeded,
    ContextOutcome,
    ContextUnavailable,
    OpportunityContext,
    OpportunityContextReader,
)
from presales.domain.contracts import AccessDenied, Actor, NotReady
from presales.domain.proposal_analysis_attempt import BackgroundRequirement
from presales.infrastructure.opportunity_workspace import (
    ReadContextRequest,
    WorkspaceAdapter,
    WorkspaceCredential,
)

JSON_KEYS = {"input_manifest"}
UUID_KEYS = {"id", "resource_id", "opportunity_id", "source_version_id", "record_id"}
TIME_KEYS = {"effective_at", "created_at", "updated_at"}

TABLE_MAP = {
    "presales.opportunity_background_records": "background",
    "presales.opportunities": "opportunities",
    "presales.resource_versions": "versions",
    "presales.resources": "resources",
    "iam.principals": "principals",
    "iam.resource_grants": "grants",
    "iam.authorization_revision": "authorization_revision",
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 11, 0, tzinfo=UTC)

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


class OfflineDatabase:
    def __init__(self, root, *, failing: bool = False) -> None:
        self.root, self.failing = Path(root), failing

    @asynccontextmanager
    async def transaction(self):
        if self.failing:
            from presales.domain.contracts import StorageUnavailable

            raise StorageUnavailable()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for schema in ("presales", "iam"):
            conn.execute(f"ATTACH DATABASE ? AS {schema}", (str(self.root / f"{schema}.db"),))

        class Connection:
            async def execute(self, statement, params=None):
                sql = str(statement)
                for qualified, table in TABLE_MAP.items():
                    sql = sql.replace(qualified, table)
                sql = " ".join(sql.split())
                for clause in (" FOR UPDATE", " FOR SHARE"):
                    sql = sql.replace(clause, "")
                sql = sql.replace("now()", "CURRENT_TIMESTAMP")
                sql = sql.replace("char_length(", "length(")
                values = {
                    key: str(value) if isinstance(value, UUID | datetime) else value
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


class Fixture:
    def __init__(self, root, actor, opportunity, source) -> None:
        self.root, self.actor = Path(root), actor
        self.opportunity, self.source = opportunity, source

    def connect(self):
        conn = sqlite3.connect(self.root / "presales.db")
        conn.row_factory = sqlite3.Row
        return conn

    def execute(self, sql, params=()):
        with self.connect() as conn:
            conn.execute(sql, params)


def build(tmp_path, monkeypatch, *, records=1, grant="opportunity", trust="confirmed"):
    async def no_lock(*args, **kwargs):
        pass

    monkeypatch.setattr("presales.access.policy.lock", no_lock)
    monkeypatch.setattr("presales.application.opportunity_context.lock", no_lock)
    tmp_path.mkdir(parents=True, exist_ok=True)
    actor, opportunity, task = uuid4(), uuid4(), uuid4()
    presales = sqlite3.connect(tmp_path / "presales.db")
    presales.executescript(
        """
        CREATE TABLE resources(id TEXT PRIMARY KEY,kind TEXT,parent_id TEXT);
        CREATE TABLE opportunities(id TEXT PRIMARY KEY,name TEXT,state TEXT);
        CREATE TABLE versions(id TEXT PRIMARY KEY,resource_id TEXT,ready INTEGER,dependencies_complete INTEGER,
            input_manifest TEXT,source_kind TEXT,declaration TEXT);
        CREATE TABLE background(id TEXT PRIMARY KEY,opportunity_id TEXT,source_version_id TEXT UNIQUE,
            kind TEXT,title TEXT,body TEXT,declaration TEXT,trust_state TEXT,effective_at TEXT,
            created_by TEXT,created_at TEXT);
        """
    )
    presales.execute("INSERT INTO resources VALUES(?, 'opportunity', NULL)", (str(opportunity),))
    presales.execute("INSERT INTO resources VALUES(?, 'task', ?)", (str(task), str(opportunity)))
    presales.execute(
        "INSERT INTO opportunities VALUES(?, 'Synthetic opportunity', 'active')",
        (str(opportunity),),
    )
    sources = []
    for index in range(records):
        source = uuid4()
        sources.append(source)
        presales.execute(
            "INSERT INTO versions VALUES(?,?,1,1,'[]','original','synthetic')",
            (str(source), str(opportunity)),
        )
        presales.execute(
            "INSERT INTO background VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                str(opportunity),
                str(source),
                "background",
                f"Synthetic background {index}",
                f"Body {index}",
                "synthetic",
                trust,
                "2026-11-01T00:00:00+00:00",
                str(actor),
                "2026-11-01T00:00:00+00:00",
            ),
        )
    presales.commit()
    presales.close()
    iam = sqlite3.connect(tmp_path / "iam.db")
    iam.executescript(
        """CREATE TABLE principals(id TEXT,role TEXT,active INTEGER);
        CREATE TABLE authorization_revision(revision INTEGER);
        CREATE TABLE grants(principal_id TEXT,resource_id TEXT);"""
    )
    iam.execute("INSERT INTO principals VALUES(?,?,1)", (str(actor), "architect"))
    iam.execute("INSERT INTO authorization_revision VALUES(1)")
    if grant == "opportunity":
        iam.execute("INSERT INTO grants VALUES(?,?)", (str(actor), str(opportunity)))
    elif grant == "task":
        iam.execute("INSERT INTO grants VALUES(?,?)", (str(actor), str(task)))
    iam.commit()
    iam.close()
    return Fixture(tmp_path, actor, opportunity, sources[0] if sources else None)


def reader(fixture, clock=None):
    return OpportunityContextReader(OfflineDatabase(fixture.root), clock=clock or Clock())


@pytest.mark.asyncio
async def test_registered_background_is_returned_with_traceable_source(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    outcome = await reader(fixture).read(
        Actor(subject=fixture.actor), fixture.opportunity, seconds=5
    )
    assert outcome.status == "content"
    source = outcome.sources[0]
    assert source.source_version_id == fixture.source
    assert source.declaration == "synthetic"
    assert source.trust_state == "confirmed"
    assert source.usable_as_fact is True


@pytest.mark.asyncio
async def test_success_empty_history_is_distinct_from_failure(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, records=0)
    outcome = await reader(fixture).read(
        Actor(subject=fixture.actor), fixture.opportunity, seconds=5
    )
    assert outcome.status == "empty"
    assert outcome.sources == ()


@pytest.mark.asyncio
async def test_pending_trust_is_returned_but_not_usable_as_fact(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, trust="pending_confirmation")
    outcome = await reader(fixture).read(
        Actor(subject=fixture.actor), fixture.opportunity, seconds=5
    )
    assert outcome.status == "content"
    assert outcome.sources[0].usable_as_fact is False


@pytest.mark.asyncio
async def test_no_grant_is_indistinguishable_from_missing_opportunity(tmp_path, monkeypatch):
    forbidden = build(tmp_path / "forbidden", monkeypatch, grant="none")
    denied = await reader(forbidden).read(
        Actor(subject=forbidden.actor), forbidden.opportunity, seconds=5
    )
    missing = await reader(forbidden).read(Actor(subject=forbidden.actor), uuid4(), seconds=5)
    assert denied.status == "not_found_or_not_allowed"
    assert missing.status == "not_found_or_not_allowed"
    assert denied.model_dump() == missing.model_dump()


@pytest.mark.asyncio
async def test_unregistered_source_fails_the_contract(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute("UPDATE versions SET ready=0")
    outcome = await reader(fixture).read(
        Actor(subject=fixture.actor), fixture.opportunity, seconds=5
    )
    assert outcome.status == "contract_failed"
    assert outcome.error_code == "context_contract_failed"


@pytest.mark.asyncio
async def test_source_pointing_at_another_opportunity_fails_the_contract(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    fixture.execute("UPDATE versions SET resource_id=?", (str(uuid4()),))
    outcome = await reader(fixture).read(
        Actor(subject=fixture.actor), fixture.opportunity, seconds=5
    )
    assert outcome.status == "contract_failed"


@pytest.mark.asyncio
async def test_deadline_expiry_returns_timeout(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    clock = Clock()
    instance = reader(fixture, clock)

    async def advance(fixture_root):
        clock.advance(30)

    original = OpportunityContextReader._read

    async def slow(self, actor, opportunity_id, deadline):
        clock.advance(30)
        return await original(self, actor, opportunity_id, deadline)

    monkeypatch.setattr(OpportunityContextReader, "_read", slow)
    outcome = await instance.read(Actor(subject=fixture.actor), fixture.opportunity, seconds=5)
    assert outcome.status == "timeout"
    assert outcome.error_code == "context_deadline_exceeded"


@pytest.mark.asyncio
async def test_storage_failure_returns_unavailable(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    failing = OpportunityContextReader(OfflineDatabase(fixture.root, failing=True), clock=Clock())
    outcome = await failing.read(Actor(subject=fixture.actor), fixture.opportunity, seconds=5)
    assert outcome.status == "unavailable"
    assert outcome.error_code == "storage_unavailable"


@pytest.mark.asyncio
async def test_required_background_failure_stops_and_optional_degrades(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)

    class Failing:
        def __init__(self) -> None:
            self.calls = 0

        async def read(self, actor, opportunity_id, *, seconds):
            self.calls += 1
            return ContextOutcome(status="unavailable", error_code="context_unavailable")

    failing = Failing()
    context = OpportunityContext(failing, clock=Clock())
    with pytest.raises(ContextUnavailable):
        await context.load(
            Actor(subject=fixture.actor),
            fixture.opportunity,
            BackgroundRequirement.REQUIRED,
        )
    assert failing.calls == READ_CONTEXT_RETRIES + 1
    degraded = await OpportunityContext(Failing(), clock=Clock()).load(
        Actor(subject=fixture.actor),
        fixture.opportunity,
        BackgroundRequirement.OPTIONAL,
    )
    assert degraded.status == "unavailable"
    assert "background_unavailable" in degraded.degradation


@pytest.mark.asyncio
async def test_unconfirmed_requirement_is_refused_before_any_read(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)

    class NeverReads:
        def __init__(self) -> None:
            self.calls = 0

        async def read(self, actor, opportunity_id, *, seconds):
            self.calls += 1
            raise AssertionError("must not read")

    probe = NeverReads()
    with pytest.raises(NotReady):
        await OpportunityContext(probe, clock=Clock()).load(
            Actor(subject=fixture.actor),
            fixture.opportunity,
            BackgroundRequirement.UNCONFIRMED,
        )
    assert probe.calls == 0


@pytest.mark.asyncio
async def test_required_background_without_task_grant_is_denied(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, grant="none")
    context = OpportunityContext(reader(fixture), clock=Clock())
    with pytest.raises(AccessDenied):
        await context.load(
            Actor(subject=fixture.actor),
            fixture.opportunity,
            BackgroundRequirement.REQUIRED,
        )


@pytest.mark.asyncio
async def test_task_grant_does_not_escalate_to_opportunity_background(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, grant="task")
    outcome = await reader(fixture).read(
        Actor(subject=fixture.actor), fixture.opportunity, seconds=5
    )
    assert outcome.status == "not_found_or_not_allowed"
    assert outcome.sources == ()
    context = OpportunityContext(reader(fixture), clock=Clock())
    with pytest.raises(AccessDenied):
        await context.load(
            Actor(subject=fixture.actor),
            fixture.opportunity,
            BackgroundRequirement.REQUIRED,
        )
    degraded = await OpportunityContext(reader(fixture), clock=Clock()).load(
        Actor(subject=fixture.actor),
        fixture.opportunity,
        BackgroundRequirement.OPTIONAL,
    )
    assert degraded.status == "not_found_or_not_allowed"
    assert degraded.sources == ()
    assert "background_not_found_or_not_allowed" in degraded.degradation


@pytest.mark.asyncio
async def test_oversized_background_is_refused_instead_of_truncated(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, records=3)
    fixture.execute("UPDATE background SET body=?", ("x" * 5000,))
    bounded = OpportunityContextReader(
        OfflineDatabase(fixture.root), max_characters=10_000, clock=Clock()
    )
    outcome = await bounded.read(Actor(subject=fixture.actor), fixture.opportunity, seconds=5)
    assert outcome.status == "capacity_exceeded"
    assert outcome.error_code == "context_capacity_exceeded"
    assert outcome.sources == ()
    assert outcome.character_count == 15_000


@pytest.mark.asyncio
async def test_background_exactly_at_the_limit_is_still_readable(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, records=3)
    fixture.execute("UPDATE background SET body=?", ("x" * 5000,))
    bounded = OpportunityContextReader(
        OfflineDatabase(fixture.root), max_characters=15_000, clock=Clock()
    )
    outcome = await bounded.read(Actor(subject=fixture.actor), fixture.opportunity, seconds=5)
    assert outcome.status == "content"
    assert outcome.character_count == 15_000
    assert len(outcome.sources) == 3


@pytest.mark.asyncio
async def test_too_many_background_records_is_refused(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch, records=3)
    bounded = OpportunityContextReader(OfflineDatabase(fixture.root), max_records=2, clock=Clock())
    outcome = await bounded.read(Actor(subject=fixture.actor), fixture.opportunity, seconds=5)
    assert outcome.status == "capacity_exceeded"
    assert outcome.sources == ()


def test_capacity_bounds_must_be_positive():
    with pytest.raises(ValueError):
        OpportunityContextReader(OfflineDatabase(Path(".")), max_characters=0)
    assert MAX_CONTEXT_CHARACTERS == 60_000
    assert MAX_CONTEXT_RECORDS == 200


@pytest.mark.asyncio
async def test_required_capacity_exceeded_stops_instead_of_degrading(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)

    class OverCapacity:
        def __init__(self) -> None:
            self.calls = 0

        async def read(self, actor, opportunity_id, *, seconds):
            self.calls += 1
            return ContextOutcome(
                status="capacity_exceeded",
                error_code="context_capacity_exceeded",
                character_count=10**6,
            )

    probe = OverCapacity()
    with pytest.raises(ContextCapacityExceeded):
        await OpportunityContext(probe, clock=Clock()).load(
            Actor(subject=fixture.actor),
            fixture.opportunity,
            BackgroundRequirement.REQUIRED,
        )
    assert probe.calls == 1
    degraded = await OpportunityContext(OverCapacity(), clock=Clock()).load(
        Actor(subject=fixture.actor),
        fixture.opportunity,
        BackgroundRequirement.OPTIONAL,
    )
    assert degraded.status == "capacity_exceeded"
    assert "background_capacity_exceeded" in degraded.degradation


def credential() -> WorkspaceCredential:
    return WorkspaceCredential(SecretStr("workspace-signing-key-long-enough-for-tests"))


class StubContext:
    def __init__(self, outcome: ContextOutcome) -> None:
        self.outcome, self.calls = outcome, []

    async def read(self, actor, opportunity_id, *, seconds):
        self.calls.append((actor.subject, opportunity_id))
        return self.outcome


@pytest.mark.asyncio
async def test_tool_trusts_only_the_injected_token(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    context = StubContext(ContextOutcome(status="empty"))
    adapter = WorkspaceAdapter(context, credential())
    request = ReadContextRequest(opportunity_id=fixture.opportunity)
    token = credential().issue(Actor(subject=fixture.actor), fixture.opportunity)
    accepted = await adapter.read_context(request, token)
    assert accepted["status"] == "empty"
    assert context.calls == [(fixture.actor, fixture.opportunity)]
    rejected = await adapter.read_context(request, "")
    assert rejected["status"] == "not_found_or_not_allowed"
    assert len(context.calls) == 1


@pytest.mark.asyncio
async def test_token_for_another_opportunity_is_rejected(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    context = StubContext(ContextOutcome(status="empty"))
    adapter = WorkspaceAdapter(context, credential())
    foreign = credential().issue(Actor(subject=fixture.actor), uuid4())
    result = await adapter.read_context(
        ReadContextRequest(opportunity_id=fixture.opportunity), foreign
    )
    assert result["status"] == "not_found_or_not_allowed"
    assert context.calls == []


@pytest.mark.asyncio
async def test_attempt_binding_is_enforced_both_ways(tmp_path, monkeypatch):
    fixture = build(tmp_path, monkeypatch)
    context = StubContext(ContextOutcome(status="empty"))
    adapter = WorkspaceAdapter(context, credential())
    attempt = uuid4()
    bound_token = credential().issue(Actor(subject=fixture.actor), fixture.opportunity, attempt)
    accepted = await adapter.read_context(
        ReadContextRequest(opportunity_id=fixture.opportunity, attempt_id=attempt), bound_token
    )
    assert accepted["status"] == "empty"
    assert context.calls == [(fixture.actor, fixture.opportunity)]
    dropped = await adapter.read_context(
        ReadContextRequest(opportunity_id=fixture.opportunity), bound_token
    )
    assert dropped["status"] == "not_found_or_not_allowed"
    unbound = credential().issue(Actor(subject=fixture.actor), fixture.opportunity)
    upgraded = await adapter.read_context(
        ReadContextRequest(opportunity_id=fixture.opportunity, attempt_id=attempt), unbound
    )
    assert upgraded["status"] == "not_found_or_not_allowed"
    assert len(context.calls) == 1


@pytest.mark.asyncio
async def test_request_rejects_caller_supplied_identity_and_endpoint(tmp_path):
    with pytest.raises(Exception):
        ReadContextRequest(opportunity_id=uuid4(), actor=str(uuid4()))
    with pytest.raises(Exception):
        ReadContextRequest(opportunity_id=uuid4(), endpoint="http://evil.example")


def test_server_registers_only_the_read_only_tool(tmp_path, monkeypatch):
    from presales.infrastructure.opportunity_workspace import create_workspace_server

    async def noop():
        return None

    server = create_workspace_server(
        WorkspaceAdapter(StubContext(ContextOutcome(status="empty")), credential())
    )
    tools = {tool.name for tool in server._tool_manager.list_tools()}
    assert tools == {"read_context"}


def test_settings_require_a_dedicated_key():
    from presales.infrastructure.opportunity_workspace import WorkspaceSettings

    with pytest.raises(Exception):
        WorkspaceCredential(SecretStr("short"))
    assert WorkspaceSettings(
        jwt_secret=SecretStr("workspace-signing-key-long-enough-for-tests")
    ).jwt_secret


def _background_row(opportunity, source, actor):
    return {
        "id": uuid4(),
        "opportunity": opportunity,
        "source": source,
        "actor": actor,
    }


async def _insert_version(conn, resource, actor, version_id=None):
    from presales.infrastructure.database import execute

    version_id = version_id or uuid4()
    await execute(
        conn,
        """INSERT INTO presales.resource_versions
        (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,
         input_manifest,created_by)
        VALUES(:id,:resource,1,'original','synthetic',false,false,'[]',:actor)""",
        id=version_id,
        resource=resource,
        actor=actor,
    )
    return version_id


INSERT_BACKGROUND = """
INSERT INTO presales.opportunity_background_records
(id,opportunity_id,source_version_id,kind,title,body,declaration,trust_state,effective_at,created_by)
VALUES(:id,:opportunity,:source,'background','Synthetic','Body','synthetic','confirmed',now(),:actor)
"""


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_background_record_requires_a_source_owned_by_that_opportunity(services):
    from presales.infrastructure.database import execute, rows
    from presales.maintenance.service import demo_id

    _, _, admin, _ = services
    opportunity, other = demo_id("opportunity-a"), demo_id("opportunity-b")
    actor = demo_id("architect")
    async with admin.transaction() as conn:
        owned = await _insert_version(conn, opportunity, actor)
        foreign = await _insert_version(conn, other, actor)
    async with admin.transaction() as conn:
        await execute(
            conn,
            INSERT_BACKGROUND,
            **_background_row(opportunity, owned, actor),
        )
    from presales.domain.contracts import StorageUnavailable

    with pytest.raises(StorageUnavailable):
        async with admin.transaction() as conn:
            await execute(
                conn,
                INSERT_BACKGROUND,
                **_background_row(opportunity, foreign, actor),
            )
    async with admin.transaction() as conn:
        stored = await rows(
            conn,
            "SELECT 1 FROM presales.opportunity_background_records"
            " WHERE opportunity_id=:id AND source_version_id=:source",
            id=opportunity,
            source=foreign,
        )
    assert stored == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_runtime_cannot_rewrite_background_content(services):
    from presales.infrastructure.database import execute, rows
    from presales.maintenance.service import demo_id

    service, _, admin, _ = services
    opportunity, actor = demo_id("opportunity-a"), demo_id("architect")
    async with admin.transaction() as conn:
        source = await _insert_version(conn, opportunity, actor)
        row = _background_row(opportunity, source, actor)
        await execute(conn, INSERT_BACKGROUND, **row)
    from presales.domain.contracts import StorageUnavailable

    with pytest.raises(StorageUnavailable):
        async with service.database.transaction() as conn:
            await execute(
                conn,
                "UPDATE presales.opportunity_background_records SET body='tampered' WHERE id=:id",
                id=row["id"],
            )
    async with admin.transaction() as conn:
        stored = (
            await rows(
                conn,
                "SELECT body FROM presales.opportunity_background_records WHERE id=:id",
                id=row["id"],
            )
        )[0]
    assert stored["body"] == "Body"
