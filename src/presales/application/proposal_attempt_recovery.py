"""Restart verification, stable recovery intent and bounded close-out of an Attempt.

This module only decides whether a reconciling Attempt may be continued. It never
re-enters execution: re-acquiring the slots and bumping the execution generation
belongs to the fenced resume step, which needs its own database gate.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from presales.access.policy import Policy
from presales.application.proposal_attempt_context import ProposalAttemptContext
from presales.application.service import audit
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    DomainError,
    NotReady,
    digest,
)
from presales.domain.proposal_analysis_attempt import (
    AttemptBudget,
    AttemptOutcome,
    AttemptStage,
    AttemptTerminal,
    FenceConflict,
    RecoveryIntent,
)
from presales.infrastructure.database import AUTH_LOCK, Database, lock, rows
from presales.infrastructure.proposal_attempt_store import ProposalAttemptStore
from presales.infrastructure.proposal_execution_records import (
    ProposalExecutionRecord,
    ProposalExecutionRecords,
)

RECOVERY_OPERATION = "recover_proposal_attempt"
RECOVERY_CLOSE_OPERATION = "close_unrecoverable_attempt"
RECOVERY_KEY_PREFIX = "recovery-intent-"


class RecoveryBlocked(DomainError):
    code = "recovery_blocked"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__()


RecoveryReason = Literal[
    "not_original_actor",
    "checkpoint_missing",
    "input_changed",
    "source_invalid",
    "budget_exhausted",
    "unknown_calls_pending",
    "execution_unconfirmed",
    "cancelled_requires_drain",
    "business_already_committed",
]


class RecoveryFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    task_id: UUID
    stage: AttemptStage
    execution_generation: int = Field(ge=1)
    run_revision: int = Field(ge=0)
    code: Literal[
        "terminal",
        "business_committed",
        "unknown_calls_pending",
        "execution_unconfirmed",
        "awaiting_verification",
        "resume_recorded",
    ]
    open_calls: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


READ_ATTEMPT_SQL = """
SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id FOR UPDATE
"""

READ_TASK_SQL = """
SELECT * FROM presales.tasks WHERE id=:task FOR UPDATE
"""

READ_OPEN_CALLS_SQL = """
SELECT record_key FROM presales.proposal_execution_records
WHERE attempt_id=:attempt AND kind='call' AND status IN ('registered','unknown')
"""

READ_RECORDS_SQL = """
SELECT * FROM presales.proposal_execution_records WHERE attempt_id=:attempt
"""

READ_CHECKPOINT_SQL = """
SELECT 1 FROM presales.proposal_checkpoint_receipts
WHERE attempt_id=:attempt AND checkpoint_id=:checkpoint
"""

READ_TASK_SLOT_SQL = """
SELECT * FROM presales.proposal_task_slots WHERE task_id=:task
"""

READ_GLOBAL_SLOT_SQL = """
SELECT * FROM presales.proposal_global_slot WHERE singleton=true
"""


def recovery_parameters(intent: RecoveryIntent) -> dict:
    return intent.model_dump(mode="json", exclude={"request_id"})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ProposalAttemptRecovery:
    """Re-authenticates, re-verifies and records a stable resumable intent."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database, self.clock = database, clock

    async def inspect(self, actor: Actor, attempt_id: UUID) -> RecoveryFinding:
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK, shared=True)
            attempt, task = await self._load(conn, actor, attempt_id)
            open_calls = tuple(await self._open_calls(conn, attempt_id))
            code, reasons = await self._classify(conn, attempt, task, open_calls)
            return RecoveryFinding(
                attempt_id=attempt_id,
                task_id=UUID(str(attempt["task_id"])),
                stage=AttemptStage(attempt["execution_stage"]),
                execution_generation=int(attempt["execution_generation"]),
                run_revision=int(attempt["run_revision"]),
                code=code,
                open_calls=open_calls,
                reasons=reasons,
            )

    async def request_recovery(self, actor: Actor, intent: RecoveryIntent) -> dict:
        fingerprint = digest({"target": str(intent.attempt_id), **recovery_parameters(intent)})
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK, shared=True)
            store = ProposalAttemptStore(conn)
            replay = await store.receipt(actor.subject, RECOVERY_OPERATION, intent.request_id)
            if replay is not None:
                if replay["parameter_digest"] != fingerprint:
                    raise Conflict()
                return dict(replay["result"])
            attempt, task = await self._load(conn, actor, intent.attempt_id)
            self._require_reconcilable(attempt, task, intent)
            records = await rows(conn, READ_RECORDS_SQL, attempt=intent.attempt_id)
            open_calls = await self._open_calls(conn, intent.attempt_id)
            if attempt["result_ready"]:
                raise RecoveryBlocked("business_already_committed")
            if open_calls:
                raise RecoveryBlocked("unknown_calls_pending")
            budget = AttemptBudget.model_validate(attempt["budget"])
            remaining = budget.max_active_seconds - ProposalAttemptContext._used(records)
            if remaining <= 0:
                raise RecoveryBlocked("budget_exhausted")
            checkpoint = await rows(
                conn,
                READ_CHECKPOINT_SQL,
                attempt=intent.attempt_id,
                checkpoint=intent.checkpoint_id,
            )
            if not checkpoint:
                raise RecoveryBlocked("checkpoint_missing")
            policy = Policy(conn, actor)
            if not await self._sources_valid(policy, conn, attempt, records):
                raise RecoveryBlocked("source_invalid")
            result = {
                "attempt_id": str(intent.attempt_id),
                "task_id": str(attempt["task_id"]),
                "request_id": str(intent.request_id),
                "execution_generation": int(attempt["execution_generation"]),
                "run_revision": int(attempt["run_revision"]),
                "checkpoint_id": intent.checkpoint_id,
                "remaining_active_seconds": remaining,
                "resume_permitted": True,
                "next_execution_generation": int(attempt["execution_generation"]) + 1,
            }
            await self._record_intent(conn, attempt, intent, result)
            await store.write_receipt(
                actor.subject, RECOVERY_OPERATION, intent.request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                RECOVERY_OPERATION,
                UUID(str(attempt["task_id"])),
                "resume_recorded",
                policy.authorization_revision,
                intent.request_id,
                int(task["revision"]),
            )
            return result

    async def close_unrecoverable(
        self,
        actor: Actor,
        attempt_id: UUID,
        *,
        request_id: UUID,
        execution_stopped: bool,
        reason: str,
        outcome: AttemptOutcome = AttemptOutcome.FAILED,
    ) -> dict:
        if outcome not in {AttemptOutcome.FAILED, AttemptOutcome.MATERIALS_REQUIRED}:
            raise ValueError("Unrecoverable close-out only ends with a failure outcome")
        fingerprint = digest(
            {
                "target": str(attempt_id),
                "request_id": str(request_id),
                "execution_stopped": execution_stopped,
                "reason": reason,
                "outcome": outcome.value,
            }
        )
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK, shared=True)
            store = ProposalAttemptStore(conn)
            replay = await store.receipt(actor.subject, RECOVERY_CLOSE_OPERATION, request_id)
            if replay is not None:
                if replay["parameter_digest"] != fingerprint:
                    raise Conflict()
                return dict(replay["result"])
            attempt, task = await self._load(conn, actor, attempt_id)
            if (
                attempt["execution_stage"] == AttemptStage.FINISHED.value
                or attempt["cancelled_at"] is not None
                or task["state"] == "cancelled"
            ):
                raise AttemptTerminal()
            if attempt["execution_stage"] != AttemptStage.RECONCILING.value:
                raise NotReady()
            open_calls = await self._open_calls(conn, attempt_id)
            if not execution_stopped or open_calls:
                raise RecoveryBlocked("execution_unconfirmed")
            generation = int(attempt["execution_generation"])
            record = ProposalExecutionRecord(
                attempt_id=attempt_id,
                kind="recovery",
                record_key=f"recovery-close-{generation}",
                execution_generation=generation,
                input_digest=digest({"reason": reason, "outcome": outcome.value}),
                status="completed",
                body={"type": "unrecoverable", "reason": reason, "outcome": outcome.value},
            )
            await ProposalExecutionRecords(conn).save(record)
            projection = await store.transition(
                attempt_id,
                from_stage=AttemptStage.RECONCILING,
                stage=AttemptStage.FINISHED,
                outcome=outcome,
                expected_generation=generation,
                expected_run_revision=int(attempt["run_revision"]),
                error_code=reason,
            )
            await store.release_slots(attempt_id, generation)
            result = {
                "attempt_id": str(attempt_id),
                "task_id": str(attempt["task_id"]),
                "stage": projection.stage.value,
                "outcome": projection.outcome.value if projection.outcome else None,
                "execution_generation": generation,
                "run_revision": projection.run_revision,
                "reason": reason,
                "slots_released": True,
            }
            await store.write_receipt(
                actor.subject, RECOVERY_CLOSE_OPERATION, request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                RECOVERY_CLOSE_OPERATION,
                UUID(str(attempt["task_id"])),
                "unrecoverable_closed",
                await self._authorization_revision(conn, actor),
                request_id,
                int(task["revision"]),
            )
            return result

    async def _load(self, conn, actor: Actor, attempt_id: UUID) -> tuple[dict, dict]:
        attempts = await rows(conn, READ_ATTEMPT_SQL, id=attempt_id)
        if not attempts:
            raise AccessDenied()
        attempt = attempts[0]
        if attempt["actor"] != actor.subject:
            raise AccessDenied()
        policy = Policy(conn, actor)
        await policy.start(architect=True)
        await policy.resource(UUID(str(attempt["task_id"])))
        tasks = await rows(conn, READ_TASK_SQL, task=attempt["task_id"])
        if not tasks:
            raise AccessDenied()
        return attempt, tasks[0]

    async def _classify(
        self, conn, attempt: dict, task: dict, open_calls: tuple[str, ...]
    ) -> tuple[
        Literal[
            "terminal",
            "business_committed",
            "unknown_calls_pending",
            "execution_unconfirmed",
            "awaiting_verification",
            "resume_recorded",
        ],
        tuple[str, ...],
    ]:
        reasons: list[str] = []
        if (
            attempt["execution_stage"] == AttemptStage.FINISHED.value
            or attempt["cancelled_at"] is not None
            or task["state"] == "cancelled"
        ):
            return "terminal", ("terminal_attempt",)
        slots = await self._slot_ownership(conn, attempt)
        if slots:
            reasons.extend(slots)
        if attempt["result_ready"]:
            return "business_committed", tuple(reasons)
        if open_calls:
            return "unknown_calls_pending", tuple(reasons) + tuple(open_calls)
        if attempt["execution_stage"] != AttemptStage.RECONCILING.value:
            return "execution_unconfirmed", tuple(reasons)
        generation = int(attempt["execution_generation"])
        pending = [
            row
            for row in await rows(
                conn,
                "SELECT * FROM presales.proposal_execution_records WHERE attempt_id=:attempt",
                attempt=attempt["id"],
            )
            if row["kind"] == "recovery"
            and str(row["record_key"]).startswith(RECOVERY_KEY_PREFIX)
            and int(row["execution_generation"]) == generation
        ]
        if pending:
            return "resume_recorded", tuple(reasons)
        return "awaiting_verification", tuple(reasons)

    async def _slot_ownership(self, conn, attempt: dict) -> list[str]:
        generation = int(attempt["execution_generation"])
        attempt_id = UUID(str(attempt["id"]))
        reasons: list[str] = []
        task_slot = (await rows(conn, READ_TASK_SLOT_SQL, task=attempt["task_id"]))[:1]
        global_slot = (await rows(conn, READ_GLOBAL_SLOT_SQL))[:1]
        if not task_slot or (
            task_slot[0]["attempt_id"],
            int(task_slot[0]["execution_generation"]),
        ) != (attempt_id, generation):
            reasons.append("task_slot_foreign")
        if (
            not global_slot
            or global_slot[0]["attempt_id"] is None
            or (
                global_slot[0]["attempt_id"],
                int(global_slot[0]["execution_generation"]),
            )
            != (attempt_id, generation)
        ):
            reasons.append("global_slot_foreign")
        return reasons

    def _require_reconcilable(self, attempt: dict, task: dict, intent: RecoveryIntent) -> None:
        if (
            attempt["execution_stage"] == AttemptStage.FINISHED.value
            or attempt["cancelled_at"] is not None
            or task["state"] == "cancelled"
        ):
            raise AttemptTerminal()
        if attempt["execution_stage"] != AttemptStage.RECONCILING.value:
            raise NotReady()
        if int(attempt["execution_generation"]) != intent.expected_execution_generation:
            raise FenceConflict()
        if int(attempt["run_revision"]) != intent.expected_run_revision:
            raise Conflict()
        if int(task["revision"]) != intent.expected_task_revision:
            raise Conflict()
        if (
            int(attempt["input_revision"]) != intent.expected_input_revision
            or int(task["input_revision"]) != intent.expected_input_revision
        ):
            raise RecoveryBlocked("input_changed")

    async def _sources_valid(self, policy: Policy, conn, attempt: dict, records) -> bool:
        scope = {UUID(str(item)) for item in attempt["start_scope"]}
        used = {UUID(str(s)) for r in records for s in (r["body"] or {}).get("sources", [])}
        try:
            for source in used | scope:
                for version_id in await policy.source_closure(source):
                    versions = await rows(
                        conn,
                        "SELECT * FROM presales.resource_versions WHERE id=:id",
                        id=version_id,
                    )
                    if not versions:
                        return False
                    if version_id not in scope and versions[0]["resource_id"] not in scope:
                        return False
                    knowledge = await rows(
                        conn,
                        "SELECT state FROM presales.knowledge_versions WHERE id=:id",
                        id=version_id,
                    )
                    if knowledge and knowledge[0]["state"] != "published":
                        return False
        except AccessDenied:
            return False
        return True

    async def _open_calls(self, conn, attempt_id: UUID) -> list[str]:
        return [
            row["record_key"] for row in await rows(conn, READ_OPEN_CALLS_SQL, attempt=attempt_id)
        ]

    async def _record_intent(
        self, conn, attempt: dict, intent: RecoveryIntent, result: dict
    ) -> None:
        body = {
            "type": "resume_intent",
            "request_id": str(intent.request_id),
            "checkpoint_id": intent.checkpoint_id,
            "input_digest": intent.input_digest,
            "expected_run_revision": intent.expected_run_revision,
            "next_execution_generation": result["next_execution_generation"],
        }
        await ProposalExecutionRecords(conn).save(
            ProposalExecutionRecord(
                attempt_id=intent.attempt_id,
                kind="recovery",
                record_key=f"{RECOVERY_KEY_PREFIX}{intent.request_id}",
                execution_generation=int(attempt["execution_generation"]),
                input_digest=digest(body),
                status="completed",
                body=body,
            )
        )

    async def _authorization_revision(self, conn, actor: Actor) -> int:
        revision = await rows(conn, "SELECT revision FROM iam.authorization_revision")
        return revision[0]["revision"] if revision else 0


__all__ = [
    "ProposalAttemptRecovery",
    "RECOVERY_CLOSE_OPERATION",
    "RECOVERY_KEY_PREFIX",
    "RECOVERY_OPERATION",
    "RecoveryBlocked",
    "RecoveryFinding",
    "RecoveryReason",
    "recovery_parameters",
]
