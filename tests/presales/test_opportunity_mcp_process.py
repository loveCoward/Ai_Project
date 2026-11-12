"""Real stdio child-process checks for the read-only Opportunity Workspace; all data synthetic."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import SecretStr

from presales.application.opportunity_context import (
    ContextOutcome,
    ContextUnavailable,
    OpportunityContext,
    OpportunityContextReader,
)
from presales.domain.contracts import AccessDenied, Actor
from presales.domain.proposal_analysis_attempt import BackgroundRequirement
from presales.infrastructure.database import execute
from presales.infrastructure.opportunity_workspace import (
    ReadContextRequest,
    WorkspaceClient,
    WorkspaceCredential,
)
from presales.maintenance.service import AuthorizationChange, demo_id

KEY = SecretStr("synthetic-workspace-signing-key-for-engineering-only")
ARCHITECT = Actor(subject=demo_id("architect"))
OPPORTUNITY = demo_id("opportunity-a")
OTHER = demo_id("opportunity-b")

INSERT_BACKGROUND = """
INSERT INTO presales.opportunity_background_records
(id,opportunity_id,source_version_id,kind,title,body,declaration,trust_state,effective_at,created_by)
VALUES(:id,:opportunity,:source,'background',:title,:body,'synthetic',:trust,now(),:actor)
"""


def _environment(runtime_url: str) -> dict[str, str]:
    environment = {
        "PRESALES_DATABASE_URL": runtime_url,
        "PRESALES_FILE_ROOT": ".presales-test-data",
        "PRESALES_WORKSPACE_JWT_SECRET": KEY.get_secret_value(),
        "OPENAI_API_KEY": "never-inherit",
        "PRESALES_JWT_SECRET": "never-inherit",
    }
    for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"):
        if name in os.environ:
            environment[name] = os.environ[name]
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return environment


class Child:
    """Raw JSON-RPC over a real child process: gives access to `_meta`, EOF and exit codes."""

    def __init__(self, environment: dict[str, str], log: Path) -> None:
        self.stderr = log.open("wb")
        self.log = log
        self.process = subprocess.Popen(
            [sys.executable, "-m", "presales.infrastructure.opportunity_workspace"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            env=environment,
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.next_id = 0

    def send(self, method: str, params: dict | None = None) -> None:
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            detail = Path(self.log).read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"child closed the stream: {detail[-2000:]}")
        answer = json.loads(line)
        assert answer.get("id") == self.next_id
        return answer

    def initialize(self) -> None:
        answer = self.call(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "synthetic-test", "version": "1"},
            },
        )
        assert "result" in answer, answer
        self.send("notifications/initialized")

    def read(self, *, token: str | None, opportunity=OPPORTUNITY, attempt=None) -> dict:
        request = {"opportunity_id": str(opportunity)}
        if attempt is not None:
            request["attempt_id"] = str(attempt)
        params = {"name": "read_context", "arguments": {"request": request}}
        if token is not None:
            params["_meta"] = {"presales_context": token}
        answer = self.call("tools/call", params)
        assert "error" not in answer, answer
        return answer["result"]

    def close(self) -> int:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.close()
        self.process.stdout.close()
        code = self.process.wait(timeout=15)
        self.stderr.close()
        return code

    def kill(self) -> int:
        self.process.kill()
        code = self.process.wait(timeout=15)
        self.stderr.close()
        return code


def payload(result: dict) -> dict:
    assert result["isError"] is False, result
    return dict(result["structuredContent"])


async def source(admin, resource, *, version_no=1):
    version = uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            """INSERT INTO presales.resource_versions
            (id,resource_id,version_no,source_kind,declaration,ready,dependencies_complete,
             input_manifest,created_by)
            VALUES(:id,:resource,:version_no,'original','synthetic',false,false,'[]',:actor)""",
            id=version,
            resource=resource,
            version_no=version_no,
            actor=ARCHITECT.subject,
        )
        await execute(
            conn,
            "UPDATE presales.resource_versions SET ready=true,dependencies_complete=true"
            " WHERE id=:id",
            id=version,
        )
    return version


async def unready_source(admin, resource):
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
            actor=ARCHITECT.subject,
        )
    return version


async def background(admin, opportunity, source_id, *, body="Synthetic body", trust="confirmed"):
    identifier = uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            INSERT_BACKGROUND,
            id=identifier,
            opportunity=opportunity,
            source=source_id,
            title="Synthetic background",
            body=body,
            trust=trust,
            actor=ARCHITECT.subject,
        )
    return identifier


async def revoke(maintenance, resource):
    change = await maintenance.preview(
        [AuthorizationChange(kind="revoke", principal_id=ARCHITECT.subject, resource_id=resource)]
    )
    await maintenance.apply(UUID(change["change_id"]), change["digest"], "Synthetic restriction")


async def grant(maintenance, resource):
    change = await maintenance.preview(
        [AuthorizationChange(kind="grant", principal_id=ARCHITECT.subject, resource_id=resource)]
    )
    await maintenance.apply(
        UUID(change["change_id"]), change["digest"], "Synthetic access", confirm_expansion=True
    )


def test_client_environment_is_minimal_and_deadline_is_bounded():
    client = WorkspaceClient(WorkspaceCredential(KEY), _environment("synthetic"))
    assert client.environment["PRESALES_DATABASE_URL"] == "synthetic"
    assert "OPENAI_API_KEY" not in client.environment
    assert "PRESALES_JWT_SECRET" not in client.environment
    assert client.environment["PYTHONPATH"].endswith("src")
    with pytest.raises(ValueError):
        WorkspaceClient(WorkspaceCredential(KEY), _environment("synthetic"), seconds=11)


def test_attempt_scoped_token_cannot_be_replayed_elsewhere():
    credential = WorkspaceCredential(KEY)
    attempt = uuid4()
    token = credential.issue(ARCHITECT, OPPORTUNITY, attempt)
    assert credential.verify(
        token, ReadContextRequest(opportunity_id=OPPORTUNITY, attempt_id=attempt)
    ).subject == (ARCHITECT.subject)
    for request in (
        ReadContextRequest(opportunity_id=OPPORTUNITY),
        ReadContextRequest(opportunity_id=OPPORTUNITY, attempt_id=uuid4()),
        ReadContextRequest(opportunity_id=OTHER, attempt_id=attempt),
    ):
        with pytest.raises(Exception):
            credential.verify(token, request)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_stdio_roundtrip_matches_direct_read_and_binds_attempt(services, tmp_path):
    _, _, admin, _ = services
    version = await source(admin, OPPORTUNITY)
    record = await background(admin, OPPORTUNITY, version, body="Synthetic roundtrip body")
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "roundtrip.log")
    try:
        child.initialize()
        tools = child.call("tools/list")["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["read_context"]
        assert tools[0]["annotations"]["readOnlyHint"] is True

        direct = await OpportunityContextReader(services[0].database).read(
            ARCHITECT, OPPORTUNITY, seconds=10
        )
        attempt = uuid4()
        token = WorkspaceCredential(KEY).issue(ARCHITECT, OPPORTUNITY, attempt)
        remote = payload(child.read(token=token, attempt=attempt))
        assert remote["status"] == "content"
        assert [item["record_id"] for item in remote["sources"]] == [str(record)]
        assert [item["record_id"] for item in remote["sources"]] == [
            str(item.record_id) for item in direct.sources
        ]
        assert remote["sources"][0]["body"] == "Synthetic roundtrip body"
        assert remote["sources"][0]["usable_as_fact"] is True

        assert payload(child.read(token=token))["status"] == "not_found_or_not_allowed"
        assert payload(child.read(token=token, attempt=uuid4()))["status"] == (
            "not_found_or_not_allowed"
        )
        forged = child.call(
            "tools/call",
            {
                "name": "read_context",
                "arguments": {
                    "request": {
                        "opportunity_id": str(OPPORTUNITY),
                        "attempt_id": str(attempt),
                        "actor": str(uuid4()),
                    }
                },
                "_meta": {"presales_context": token},
            },
        )["result"]
        assert forged["isError"] is True
    finally:
        assert child.close() == 0
    text = Path(child.log).read_text(encoding="utf-8", errors="replace")
    assert "Synthetic roundtrip body" not in text


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_stdio_seven_outcomes_of_authorization_and_trust(services, tmp_path):
    _, maintenance, admin, _ = services
    version = await source(admin, OPPORTUNITY)
    await background(admin, OPPORTUNITY, version, trust="pending_confirmation")
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "outcomes.log")
    credential = WorkspaceCredential(KEY)
    try:
        child.initialize()
        pending = payload(child.read(token=credential.issue(ARCHITECT, OPPORTUNITY)))
        assert pending["status"] == "content"
        assert pending["sources"][0]["trust_state"] == "pending_confirmation"
        assert pending["sources"][0]["usable_as_fact"] is False
        assert payload(child.read(token=credential.issue(ARCHITECT, uuid4())))["status"] == (
            "not_found_or_not_allowed"
        )
        assert payload(child.read(token="forged"))["status"] == "not_found_or_not_allowed"
        assert (
            payload(child.read(token=credential.issue(Actor(subject=uuid4()), OPPORTUNITY)))[
                "status"
            ]
            == "not_found_or_not_allowed"
        )
        assert payload(child.read(token=None))["status"] == "not_found_or_not_allowed"

        await revoke(maintenance, OPPORTUNITY)
        assert payload(child.read(token=credential.issue(ARCHITECT, OPPORTUNITY)))["status"] == (
            "not_found_or_not_allowed"
        )
    finally:
        assert child.close() == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_stdio_empty_history_is_success_not_a_failure(services, tmp_path):
    _, maintenance, admin, _ = services
    empty_opportunity = uuid4()
    async with admin.transaction() as conn:
        await execute(
            conn,
            "INSERT INTO presales.resources VALUES(:id,'opportunity',NULL)",
            id=empty_opportunity,
        )
        await execute(
            conn,
            "INSERT INTO presales.opportunities(id,name,created_by,state)"
            " VALUES(:id,'Synthetic empty',:actor,'active')",
            id=empty_opportunity,
            actor=ARCHITECT.subject,
        )
    await grant(maintenance, empty_opportunity)
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "empty.log")
    try:
        child.initialize()
        token = WorkspaceCredential(KEY).issue(ARCHITECT, empty_opportunity)
        result = payload(child.read(token=token, opportunity=empty_opportunity))
        assert result["status"] == "empty"
        assert result["sources"] == []
        assert result["error_code"] is None
    finally:
        assert child.close() == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_stdio_contract_failure_is_reported_as_contract_failed(services, tmp_path):
    _, _, admin, _ = services
    version = await unready_source(admin, OPPORTUNITY)
    await background(admin, OPPORTUNITY, version)
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "contract.log")
    credential = WorkspaceCredential(KEY)
    try:
        child.initialize()
        failed = payload(child.read(token=credential.issue(ARCHITECT, OPPORTUNITY)))
        assert failed["status"] == "contract_failed"
        assert failed["error_code"] == "context_contract_failed"
        assert failed["sources"] == []
    finally:
        assert child.close() == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_stdio_refuses_direct_write_tool_calls(services, tmp_path):
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "write.log")
    token = WorkspaceCredential(KEY).issue(ARCHITECT, OPPORTUNITY)
    try:
        child.initialize()
        for name in ("write_context", "delete_context", "publish_knowledge"):
            answer = child.call(
                "tools/call",
                {"name": name, "arguments": {}, "_meta": {"presales_context": token}},
            )
            assert "error" not in answer, answer
            assert answer["result"]["isError"] is True
            assert "unknown tool" in answer["result"]["content"][0]["text"].lower()
    finally:
        assert child.close() == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_stdio_capacity_exceeded_refuses_without_returning_partial_content(
    services, tmp_path
):
    _, _, admin, _ = services
    bodies = ["x" * 20_000, "y" * 20_000, "z" * 20_000, "w" * 20_000]
    for index, body in enumerate(bodies, start=1):
        await background(
            admin, OPPORTUNITY, await source(admin, OPPORTUNITY, version_no=index), body=body
        )
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "capacity.log")
    credential = WorkspaceCredential(KEY)
    try:
        child.initialize()
        refused = payload(child.read(token=credential.issue(ARCHITECT, OPPORTUNITY)))
        assert refused["status"] == "capacity_exceeded"
        assert refused["error_code"] == "context_capacity_exceeded"
        assert refused["sources"] == []
        assert refused["character_count"] == 80_000
    finally:
        assert child.close() == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_child_reports_unavailable_when_database_is_unreachable(tmp_path):
    environment = _environment("postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/none")
    result = await WorkspaceClient(WorkspaceCredential(KEY), environment).read_context(
        ARCHITECT, ReadContextRequest(opportunity_id=OPPORTUNITY)
    )
    assert result.status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_client_deadline_expiry_is_reported_as_timeout(tmp_path):
    client = WorkspaceClient(
        WorkspaceCredential(KEY),
        _environment(os.environ.get("PRESALES_TEST_RUNTIME_URL", "synthetic")),
        seconds=0.001,
    )
    result = await client.read_context(ARCHITECT, ReadContextRequest(opportunity_id=OPPORTUNITY))
    assert result.status == "timeout"
    assert result.error_code == "context_deadline_exceeded"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_eof_and_exit_are_deterministic_and_process_is_reaped(services, tmp_path):
    _, _, _, _ = services
    log = tmp_path / "lifecycle.log"
    child = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), log)
    child.initialize()
    token = WorkspaceCredential(KEY).issue(ARCHITECT, OPPORTUNITY)
    assert payload(child.read(token=token))["status"] == "empty"
    code = child.close()
    assert code == 0
    assert child.process.poll() == 0
    started = time.monotonic()
    while time.monotonic() - started < 5:
        assert child.process.poll() is not None
        break

    crashed = Child(_environment(os.environ["PRESALES_TEST_RUNTIME_URL"]), tmp_path / "crash.log")
    crashed.initialize()
    assert crashed.kill() != 0
    assert crashed.process.poll() is not None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_required_policy_fails_closed_and_optional_degrades(services, tmp_path):
    _, _, _, _ = services

    class Unavailable:
        def __init__(self) -> None:
            self.calls = 0

        async def read(self, actor, opportunity_id, *, seconds):
            self.calls += 1
            return ContextOutcome(status="unavailable", error_code="storage_unavailable")

    required = Unavailable()
    with pytest.raises(ContextUnavailable):
        await OpportunityContext(required).load(
            ARCHITECT, OPPORTUNITY, BackgroundRequirement.REQUIRED
        )
    assert required.calls == 3
    optional = await OpportunityContext(Unavailable()).load(
        ARCHITECT, OPPORTUNITY, BackgroundRequirement.OPTIONAL
    )
    assert optional.status == "unavailable"
    assert "background_unavailable" in optional.degradation

    class NoGrant:
        async def read(self, actor, opportunity_id, *, seconds):
            return ContextOutcome(status="not_found_or_not_allowed")

    with pytest.raises(AccessDenied):
        await OpportunityContext(NoGrant()).load(
            ARCHITECT, OPPORTUNITY, BackgroundRequirement.REQUIRED
        )
