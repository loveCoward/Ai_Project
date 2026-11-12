"""Fenced authorization boundaries and a durable, conservative Attempt budget ledger."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from re import fullmatch
from typing import Literal
from uuid import UUID, uuid4

from presales.access.policy import Policy
from presales.domain.contracts import AccessDenied, Actor, Conflict, DomainError, NotReady, digest
from presales.domain.proposal_analysis_attempt import (
    AttemptBudget,
    AttemptIdentity,
    AttemptTerminal,
    FenceConflict,
)
from presales.infrastructure.database import AUTH_LOCK, Database, execute, lock, rows
from presales.infrastructure.proposal_execution_records import (
    ProposalExecutionRecord,
    ProposalExecutionRecords,
)


class BudgetExhausted(DomainError):
    code = "attempt_budget_exhausted"


class CallUnresolved(DomainError):
    code = "attempt_call_unresolved"


class ProposalAttemptContext:
    def __init__(
        self,
        database: Database,
        identity: AttemptIdentity,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database, self.identity, self.clock = database, identity, clock

    @asynccontextmanager
    async def boundary(self, sources: tuple[UUID, ...] = ()) -> AsyncIterator[tuple]:
        denied = False
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK, shared=True)
            global_slot = await rows(conn, "SELECT * FROM presales.proposal_global_slot FOR SHARE")
            tasks = await rows(
                conn,
                "SELECT * FROM presales.tasks WHERE id=:id FOR SHARE",
                id=self.identity.task_id,
            )
            slots = await rows(
                conn,
                "SELECT * FROM presales.proposal_task_slots WHERE task_id=:id FOR SHARE",
                id=self.identity.task_id,
            )
            attempts = await rows(
                conn,
                "SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id FOR UPDATE",
                id=self.identity.attempt_id,
            )
            if not attempts or not tasks:
                raise AccessDenied()
            attempt = attempts[0]
            expected = self.identity.model_dump(exclude={"attempt_id", "actor_id"})
            expected["actor"] = self.identity.actor_id
            if any(attempt[key] != value for key, value in expected.items()):
                raise FenceConflict()
            if tasks[0]["input_revision"] != self.identity.input_revision:
                raise Conflict()
            if (
                attempt["execution_stage"] == "finished"
                or attempt["cancelled_at"]
                or tasks[0]["state"] == "cancelled"
            ):
                raise AttemptTerminal()
            if attempt["execution_stage"] != "running" or tasks[0]["state"] != "analyzing":
                raise NotReady()
            if any(
                not slot
                or (slot[0]["attempt_id"], slot[0]["execution_generation"])
                != (
                    self.identity.attempt_id,
                    self.identity.execution_generation,
                )
                for slot in (global_slot, slots)
            ):
                raise FenceConflict()
            records = await rows(
                conn,
                "SELECT * FROM presales.proposal_execution_records WHERE attempt_id=:id",
                id=self.identity.attempt_id,
            )
            used = {UUID(s) for r in records for s in r["body"].get("sources", [])}
            scope = {UUID(s) for s in attempt["start_scope"]}
            policy = Policy(conn, Actor(subject=self.identity.actor_id))
            try:
                await policy.start(architect=True)
                await policy.resource(self.identity.task_id)
                for source in used | set(sources):
                    closure = await policy.source_closure(source)
                    for version_id in closure:
                        version = (
                            await rows(
                                conn,
                                "SELECT * FROM presales.resource_versions WHERE id=:id",
                                id=version_id,
                            )
                        )[0]
                        if version_id not in scope and version["resource_id"] not in scope:
                            raise AccessDenied()
                        knowledge = await rows(
                            conn,
                            "SELECT state FROM presales.knowledge_versions WHERE id=:id",
                            id=version_id,
                        )
                        if knowledge and knowledge[0]["state"] != "published":
                            raise AccessDenied()
            except AccessDenied:
                await execute(
                    conn,
                    """UPDATE presales.proposal_analysis_attempts
                    SET execution_stage='finished',outcome='authorization_invalid',
                    cancelled_at=now(),terminal_at=now(),run_revision=run_revision+1,
                    error_code='not_found_or_not_allowed',updated_at=now() WHERE id=:id""",
                    id=self.identity.attempt_id,
                )
                denied = True
            if not denied:
                yield conn, attempt, records
        if denied:
            raise AccessDenied()

    async def check(self, sources: tuple[UUID, ...] = ()) -> None:
        async with self.boundary(sources) as (conn, _, records):
            if any(
                r["body"].get("type") == "active"
                and r["status"] != "completed"
                and self.clock().timestamp() >= r["body"]["deadline"]
                for r in records
            ):
                raise BudgetExhausted()
            if sources:
                store = ProposalExecutionRecords(conn)
                key = "sources-" + digest(sorted(map(str, sources)))
                if await store.read(self.identity.attempt_id, "recovery", key):
                    return
                await store.save(
                    self._record(
                        "recovery",
                        key,
                        {"sources": list(map(str, sources))},
                        status="completed",
                    )
                )

    def _record(
        self, kind, key, body, *, status: Literal["registered", "completed"] = "registered"
    ) -> ProposalExecutionRecord:
        return ProposalExecutionRecord(
            attempt_id=self.identity.attempt_id,
            kind=kind,
            record_key=key,
            execution_generation=self.identity.execution_generation,
            input_digest=digest(body),
            status=status,
            body=body,
        )

    @staticmethod
    def _used(records: list[dict]) -> float:
        intervals = sorted(
            (r["body"]["start"], r["body"].get("end", r["body"]["deadline"]))
            for r in records
            if r["body"].get("type") == "active"
        )
        total, right = 0.0, float("-inf")
        for start, end in intervals:
            total += max(0, end - max(start, right))
            right = max(right, end)
        return total

    async def begin_active(self) -> ProposalExecutionRecord:
        async with self.boundary() as (conn, attempt, records):
            budget = AttemptBudget.model_validate(attempt["budget"])
            if any(
                r["body"].get("type") == "active" and r["status"] != "completed" for r in records
            ):
                raise CallUnresolved()
            remaining = budget.max_active_seconds - self._used(records)
            if remaining <= 0:
                raise BudgetExhausted()
            start = self.clock().timestamp()
            return await ProposalExecutionRecords(conn).save(
                self._record(
                    "recovery",
                    f"active-{uuid4()}",
                    {"type": "active", "start": start, "deadline": start + remaining},
                )
            )

    async def _active(self, conn, records, active):
        stored = await ProposalExecutionRecords(conn).read(
            self.identity.attempt_id, "recovery", active.record_key
        )
        if stored != active or active.execution_generation != self.identity.execution_generation:
            raise FenceConflict()
        now = self.clock().timestamp()
        if now < active.body["start"] or now >= active.body["deadline"]:
            raise BudgetExhausted()

    async def end_active(self, active: ProposalExecutionRecord) -> None:
        async with self.boundary() as (conn, _, records):
            stored = await ProposalExecutionRecords(conn).read(
                self.identity.attempt_id, "recovery", active.record_key
            )
            if stored != active:
                raise Conflict()
            if any(
                r["kind"] == "call" and r["status"] in {"registered", "unknown"} for r in records
            ):
                raise CallUnresolved()
            end = max(active.body["start"], min(self.clock().timestamp(), active.body["deadline"]))
            await ProposalExecutionRecords(conn).complete(
                active, status="completed", body={**active.body, "end": end}
            )

    async def register_call(
        self,
        active: ProposalExecutionRecord,
        node: str,
        kind: Literal["model", "mcp"],
        sources: tuple[UUID, ...],
        parameter_digest: str,
    ) -> ProposalExecutionRecord:
        if (
            not node
            or len(node) > 200
            or kind not in {"model", "mcp"}
            or not fullmatch(r"[0-9a-f]{64}", parameter_digest)
        ):
            raise ValueError("Invalid call identity")
        async with self.boundary(sources) as (conn, attempt, records):
            await self._active(conn, records, active)
            calls = [
                r
                for r in records
                if r["kind"] == "call"
                and r["body"].get("node") == node
                and r["body"].get("type") == kind
            ]
            if any(r["status"] in {"registered", "unknown"} for r in calls):
                raise CallUnresolved()
            if any(r["body"]["parameter_digest"] != parameter_digest for r in calls):
                raise Conflict()
            if any(r["status"] == "completed" or not r["body"].get("retryable") for r in calls):
                raise Conflict()
            budget = AttemptBudget.model_validate(attempt["budget"])
            if len(calls) >= 1 + budget.max_mcp_retries:
                raise BudgetExhausted()
            seconds = budget.max_model_seconds if kind == "model" else budget.max_mcp_seconds
            now = self.clock().timestamp()
            body = {
                "type": kind,
                "node": node,
                "sources": list(map(str, sources)),
                "start": now,
                "deadline": min(now + seconds, active.body["deadline"]),
                "parameter_digest": parameter_digest,
                "ordinal": len(calls) + 1,
            }
            return await ProposalExecutionRecords(conn).save(
                self._record("call", str(uuid4()), body)
            )

    async def finish_call(
        self,
        call: ProposalExecutionRecord,
        *,
        status: Literal["completed", "failed", "unknown"] = "completed",
        retryable=False,
    ) -> None:
        async with self.boundary(tuple(UUID(s) for s in call.body["sources"])) as (conn, _, _):
            stored = await ProposalExecutionRecords(conn).read(
                self.identity.attempt_id, "call", call.record_key
            )
            if stored != call:
                raise Conflict()
            late = self.clock().timestamp() >= call.body["deadline"]
            await ProposalExecutionRecords(conn).complete(
                call,
                status="failed" if late and status == "completed" else status,
                body={**call.body, "retryable": retryable or late, "end": self.clock().timestamp()},
            )
        if late and status == "completed":
            raise BudgetExhausted()

    async def remaining(self, active: ProposalExecutionRecord) -> float:
        async with self.boundary() as (conn, _, records):
            await self._active(conn, records, active)
            return active.body["deadline"] - self.clock().timestamp()
