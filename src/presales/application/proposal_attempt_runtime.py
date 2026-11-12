"""Single-executor supervisor that leases accepted Attempts independently of requests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from presales.domain.contracts import (
    AccessDenied,
    Conflict,
    DomainError,
    NotReady,
    StorageUnavailable,
    digest,
)
from presales.domain.proposal_analysis_attempt import (
    AttemptBusy,
    AttemptIdentity,
    AttemptOutcome,
    AttemptStage,
    AttemptTerminal,
    FenceConflict,
)
from presales.infrastructure.database import Database, rows
from presales.infrastructure.proposal_attempt_store import ProposalAttemptStore
from presales.infrastructure.proposal_execution_records import (
    ProposalExecutionRecord,
    ProposalExecutionRecords,
)


class AnalysisRuntimeUnavailable(DomainError):
    code = "analysis_runtime_unavailable"


class SupervisorStopped(DomainError):
    code = "supervisor_stopped"


class SupervisorSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    heartbeat_seconds: int = Field(default=5, ge=1, le=60)
    lease_seconds: int = Field(default=30, ge=10, le=600)
    shutdown_seconds: float = Field(default=10.0, gt=0, le=120)

    @model_validator(mode="after")
    def heartbeat_keeps_lease_alive(self) -> "SupervisorSettings":
        if self.heartbeat_seconds * 2 > self.lease_seconds:
            raise ValueError("heartbeat must be at most half of the lease")
        return self


class AttemptRunner(Protocol):
    async def run(self, identity: AttemptIdentity) -> None: ...


class ScanFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    task_id: UUID
    stage: AttemptStage
    execution_generation: int = Field(ge=1)
    run_revision: int = Field(ge=0)
    code: Literal[
        "accepted_not_dispatched",
        "needs_reconciliation",
        "awaiting_reconciliation",
        "slot_missing",
    ]
    owner: UUID | None = None
    lease_until: datetime | None = None
    lease_expired: bool = False


@dataclass(frozen=True)
class AttemptClaim:
    identity: AttemptIdentity
    run_revision: int


class DrainStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    task_id: UUID
    stage: AttemptStage
    outcome: AttemptOutcome | None
    run_revision: int
    execution_generation: int
    open_calls: tuple[str, ...] = ()
    slots_released: bool = False


READ_ATTEMPT_SQL = """
SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id FOR UPDATE
"""

CLAIM_STAGE_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage='running',run_revision=run_revision+1,error_code=NULL,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND execution_stage='accepted' AND cancelled_at IS NULL AND outcome IS NULL
"""

READ_PENDING_INTENT_SQL = """
SELECT record_key,body FROM presales.proposal_execution_records
WHERE attempt_id=:attempt AND kind='recovery'
  AND record_key LIKE 'recovery-intent-%' AND execution_generation=:generation
  AND status='completed'
ORDER BY record_key
"""

HEARTBEAT_TASK_SQL = """
UPDATE presales.proposal_task_slots SET lease_until=:lease_until
WHERE attempt_id=:attempt AND execution_generation=:generation
"""

HEARTBEAT_GLOBAL_SQL = """
UPDATE presales.proposal_global_slot SET lease_until=:lease_until
WHERE attempt_id=:attempt AND execution_generation=:generation
"""

SETTLE_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage='reconciling',run_revision=run_revision+1,error_code=:code,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision AND execution_stage='running'
  AND cancelled_at IS NULL
"""

SCAN_MARK_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage='reconciling',run_revision=run_revision+1,error_code=:code,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision
  AND execution_stage IN ('accepted','running','waiting_human')
  AND cancelled_at IS NULL
"""

READ_UNFINISHED_SQL = """
SELECT * FROM presales.proposal_analysis_attempts
WHERE execution_stage<>'finished' ORDER BY created_at,id
"""

READ_TASK_SLOT_SQL = """
SELECT * FROM presales.proposal_task_slots WHERE task_id=:task
"""

READ_GLOBAL_SLOT_SQL = """
SELECT * FROM presales.proposal_global_slot WHERE singleton=true
"""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ProposalAttemptSupervisor:
    """Leases accepted Attempts so accepted work no longer depends on a request."""

    def __init__(
        self,
        database: Database,
        *,
        runner: AttemptRunner | None = None,
        settings: SupervisorSettings | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self.database, self.runner, self.clock = database, runner, clock
        self.settings = settings or SupervisorSettings()
        self._accepting = False
        self._halted = False
        self._wake = asyncio.Event()
        self._pending: set[UUID] = set()
        self._active: dict[UUID, AttemptClaim] = {}
        self._tasks: list[asyncio.Task] = []
        self._serve_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def accepting(self) -> bool:
        return self._accepting and not self._halted

    @property
    def halted(self) -> bool:
        return self._halted

    async def start(self) -> None:
        if self._accepting:
            return
        self._accepting = True
        try:
            await self.scan()
        except StorageUnavailable:
            self._halted = True
        self._serve_task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        self._accepting = False
        self._pending.clear()
        self._wake.set()
        for task in (self._serve_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            _, running = await asyncio.wait(pending, timeout=self.settings.shutdown_seconds)
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        for claim in list(self._active.values()):
            await self._settle(claim, "shutdown_unconfirmed")
        self._active.clear()
        self._tasks.clear()
        self._serve_task = self._heartbeat_task = None

    async def wait_idle(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def notify(self, attempt_id: UUID) -> None:
        self._ensure_dispatching()
        self._pending.add(attempt_id)
        self._wake.set()

    async def dispatch(self, attempt_id: UUID) -> AttemptClaim:
        self._ensure_dispatching()
        claim = await self.claim(attempt_id)
        await self._run(claim)
        return claim

    async def submit(self, attempt_id: UUID) -> None:
        self._ensure_dispatching()
        self._tasks.append(asyncio.create_task(self._dispatch_guarded(attempt_id)))

    async def claim(self, attempt_id: UUID) -> AttemptClaim:
        if self._halted:
            raise StorageUnavailable()
        try:
            async with self.database.transaction() as conn:
                return await self._claim(conn, attempt_id)
        except StorageUnavailable:
            self._halted = True
            raise

    async def heartbeat(self, attempt_id: UUID) -> datetime:
        if self._halted:
            raise StorageUnavailable()
        try:
            async with self.database.transaction() as conn:
                attempt = await self._attempt(conn, attempt_id)
                stage, generation = attempt["execution_stage"], attempt["execution_generation"]
                if stage == AttemptStage.FINISHED.value or attempt["cancelled_at"] is not None:
                    raise AttemptTerminal()
                if stage != AttemptStage.RUNNING.value:
                    raise NotReady()
                lease = self.clock() + timedelta(seconds=self.settings.lease_seconds)
                parameters = {
                    "attempt": attempt_id,
                    "generation": generation,
                    "lease_until": lease,
                }
                task_rows = (await conn.execute(text(HEARTBEAT_TASK_SQL), parameters)).rowcount
                global_rows = (await conn.execute(text(HEARTBEAT_GLOBAL_SQL), parameters)).rowcount
                if task_rows != 1 or global_rows != 1:
                    raise FenceConflict()
                return lease
        except StorageUnavailable:
            self._halted = True
            raise

    async def scan(self) -> list[ScanFinding]:
        if self._halted:
            raise StorageUnavailable()
        try:
            async with self.database.transaction() as conn:
                return await self._scan(conn)
        except StorageUnavailable:
            self._halted = True
            raise

    async def cancel(self, attempt_id: UUID) -> DrainStatus:
        """Stops local scheduling for a cancelled Attempt; never releases slots by itself."""
        attempt = await self._cancel_state(attempt_id)
        self._pending.discard(attempt_id)
        claim = self._active.get(attempt_id)
        if claim is not None:
            await self._settle(claim, "cancelled")
        return attempt

    async def reconcile_cancelled(
        self, attempt_id: UUID, *, execution_stopped: bool
    ) -> DrainStatus:
        """Finishes a cancelled Attempt only after its local work is confirmed stopped."""
        attempt = await self._cancel_state(attempt_id)
        if attempt.outcome == AttemptOutcome.CANCELLED:
            return attempt.model_copy(
                update={"slots_released": attempt.stage == AttemptStage.FINISHED}
            )
        if not execution_stopped or attempt.open_calls:
            return attempt
        generation = attempt.execution_generation
        try:
            async with self.database.transaction() as conn:
                store = ProposalAttemptStore(conn)
                row = await store.attempt_for_cancel(attempt_id)
                if row is None:
                    raise AccessDenied()
                if row["cancelled_at"] is None:
                    raise NotReady()
                if row["execution_stage"] == AttemptStage.FINISHED.value:
                    outcome = row["outcome"]
                    return DrainStatus(
                        attempt_id=attempt_id,
                        task_id=attempt.task_id,
                        stage=AttemptStage.FINISHED,
                        outcome=AttemptOutcome(outcome),
                        run_revision=int(row["run_revision"]),
                        execution_generation=int(row["execution_generation"]),
                        slots_released=False,
                    )
                projection = await store.finish_cancelled_attempt(
                    attempt_id,
                    expected_generation=generation,
                    expected_run_revision=int(row["run_revision"]),
                )
                await store.release_slots(attempt_id, generation)
                return DrainStatus(
                    attempt_id=attempt_id,
                    task_id=attempt.task_id,
                    stage=projection.stage,
                    outcome=projection.outcome,
                    run_revision=projection.run_revision,
                    execution_generation=generation,
                    slots_released=True,
                )
        except StorageUnavailable:
            self._halted = True
            raise

    async def _cancel_state(self, attempt_id: UUID) -> DrainStatus:
        try:
            async with self.database.transaction() as conn:
                store = ProposalAttemptStore(conn)
                row = await store.attempt_for_cancel(attempt_id)
                if row is None:
                    raise AccessDenied()
                if row["cancelled_at"] is None:
                    raise NotReady()
                open_calls = tuple(await store.open_calls(attempt_id))
                task_slot = await store.task_slot_for_cancel(row["task_id"])
                global_slot = await store.global_slot_for_cancel()
                released = task_slot is None and global_slot["attempt_id"] is None
                return DrainStatus(
                    attempt_id=attempt_id,
                    task_id=row["task_id"],
                    stage=AttemptStage(row["execution_stage"]),
                    outcome=AttemptOutcome(row["outcome"]) if row["outcome"] else None,
                    run_revision=int(row["run_revision"]),
                    execution_generation=int(row["execution_generation"]),
                    open_calls=open_calls,
                    slots_released=released,
                )
        except StorageUnavailable:
            self._halted = True
            raise

    async def _claim(self, conn: AsyncConnection, attempt_id: UUID) -> AttemptClaim:
        attempt = await self._attempt(conn, attempt_id)
        stage, generation = attempt["execution_stage"], int(attempt["execution_generation"])
        if stage == AttemptStage.FINISHED.value or attempt["cancelled_at"] is not None:
            raise AttemptTerminal()
        if stage == AttemptStage.RUNNING.value:
            raise AttemptBusy()
        if stage == AttemptStage.RECONCILING.value:
            return await self._claim_resumed(conn, attempt, generation)
        if stage != AttemptStage.ACCEPTED.value:
            raise NotReady()
        await self._require_slots(conn, attempt_id, attempt["task_id"], generation)
        changed = (
            await conn.execute(
                text(CLAIM_STAGE_SQL),
                {"attempt": attempt_id, "generation": generation},
            )
        ).rowcount
        if changed != 1:
            raise Conflict()
        return AttemptClaim(
            identity=AttemptIdentity(
                attempt_id=attempt_id,
                task_id=attempt["task_id"],
                actor_id=attempt["actor"],
                thread_id=attempt["thread_id"],
                input_revision=int(attempt["input_revision"]),
                execution_generation=generation,
            ),
            run_revision=int(attempt["run_revision"]) + 1,
        )

    async def _claim_resumed(
        self, conn: AsyncConnection, attempt: dict, generation: int
    ) -> AttemptClaim:
        attempt_id = UUID(str(attempt["id"]))
        intents = await rows(
            conn, READ_PENDING_INTENT_SQL, attempt=attempt_id, generation=generation
        )
        if not intents:
            raise NotReady()
        intent = intents[0]
        request_id = UUID(str(intent["record_key"]).removeprefix("recovery-intent-"))
        task = (
            await rows(
                conn,
                "SELECT * FROM presales.tasks WHERE id=:task",
                task=attempt["task_id"],
            )
        )[:1]
        if not task:
            raise AccessDenied()
        if task[0]["state"] == "cancelled":
            raise AttemptTerminal()
        resumed = await ProposalAttemptStore(conn).resume_attempt(
            attempt_id,
            actor=attempt["actor"],
            expected_generation=generation,
            expected_run_revision=int(attempt["run_revision"]),
            expected_task_revision=int(task[0]["revision"]),
            expected_input_revision=int(attempt["input_revision"]),
            intent_request=request_id,
            lease_seconds=self.settings.lease_seconds,
        )
        return AttemptClaim(
            identity=AttemptIdentity(
                attempt_id=attempt_id,
                task_id=attempt["task_id"],
                actor_id=attempt["actor"],
                thread_id=attempt["thread_id"],
                input_revision=int(attempt["input_revision"]),
                execution_generation=resumed,
            ),
            run_revision=int(attempt["run_revision"]) + 1,
        )

    async def _scan(self, conn: AsyncConnection) -> list[ScanFinding]:
        now = self.clock()
        findings: list[ScanFinding] = []
        for attempt in await rows(conn, READ_UNFINISHED_SQL):
            attempt_id = UUID(str(attempt["id"]))
            generation = int(attempt["execution_generation"])
            stage = AttemptStage(attempt["execution_stage"])
            task_slot = (await rows(conn, READ_TASK_SLOT_SQL, task=attempt["task_id"]))[:1]
            global_slot = (await rows(conn, READ_GLOBAL_SLOT_SQL))[:1]
            owned = (
                bool(task_slot)
                and bool(global_slot)
                and (task_slot[0]["attempt_id"], int(task_slot[0]["execution_generation"]))
                == (attempt_id, generation)
                and (global_slot[0]["attempt_id"], int(global_slot[0]["execution_generation"]))
                == (attempt_id, generation)
            )
            lease_until = task_slot[0]["lease_until"] if task_slot else None
            expired = bool(lease_until) and _aware(lease_until) <= now
            code: str
            if not owned:
                code = "slot_missing"
            elif attempt["cancelled_at"] is not None:
                code = "awaiting_reconciliation"
            elif stage == AttemptStage.ACCEPTED:
                code = "accepted_not_dispatched"
            elif stage == AttemptStage.RECONCILING:
                code = "awaiting_reconciliation"
            else:
                code = "needs_reconciliation"
            run_revision = int(attempt["run_revision"])
            if expired and stage in {
                AttemptStage.RUNNING,
                AttemptStage.WAITING_HUMAN,
            }:
                changed = (
                    await conn.execute(
                        text(SCAN_MARK_SQL),
                        {
                            "attempt": attempt_id,
                            "generation": generation,
                            "expected_run_revision": run_revision,
                            "code": "restart_unconfirmed",
                        },
                    )
                ).rowcount
                if changed == 1:
                    run_revision += 1
                    code = "needs_reconciliation"
            finding = ScanFinding(
                attempt_id=attempt_id,
                task_id=attempt["task_id"],
                stage=stage,
                execution_generation=generation,
                run_revision=run_revision,
                code=code,
                owner=task_slot[0]["owner"] if task_slot else None,
                lease_until=lease_until,
                lease_expired=expired,
            )
            if attempt["cancelled_at"] is None:
                await ProposalExecutionRecords(conn).save(
                    ProposalExecutionRecord(
                        attempt_id=attempt_id,
                        kind="recovery",
                        record_key=f"supervisor-scan-{generation}-{finding.code}",
                        execution_generation=generation,
                        input_digest=digest(finding.model_dump(mode="json")),
                        status="completed",
                        body={"type": "supervisor_scan", **finding.model_dump(mode="json")},
                    )
                )
            findings.append(finding)
        return findings

    async def _attempt(self, conn: AsyncConnection, attempt_id: UUID) -> dict:
        attempts = await rows(conn, READ_ATTEMPT_SQL, id=attempt_id)
        if not attempts:
            raise AccessDenied()
        return attempts[0]

    async def _require_slots(
        self, conn: AsyncConnection, attempt_id: UUID, task_id: UUID, generation: int
    ) -> None:
        task_slot = (await rows(conn, READ_TASK_SLOT_SQL, task=task_id))[:1]
        global_slot = (await rows(conn, READ_GLOBAL_SLOT_SQL))[:1]
        if not task_slot or not global_slot:
            raise FenceConflict()
        for slot in (task_slot[0], global_slot[0]):
            if (slot["attempt_id"], int(slot["execution_generation"])) != (attempt_id, generation):
                raise FenceConflict()
            if slot["lease_until"] is not None and _aware(slot["lease_until"]) <= self.clock():
                raise NotReady()

    async def _settle(self, claim: AttemptClaim, code: str) -> None:
        identity = claim.identity
        try:
            async with self.database.transaction() as conn:
                changed = (
                    await conn.execute(
                        text(SETTLE_SQL),
                        {
                            "attempt": identity.attempt_id,
                            "generation": identity.execution_generation,
                            "expected_run_revision": claim.run_revision,
                            "code": code,
                        },
                    )
                ).rowcount
                if changed != 1:
                    raise FenceConflict()
        except DomainError:
            return

    async def _run(self, claim: AttemptClaim) -> None:
        if self.runner is None:
            raise AnalysisRuntimeUnavailable()
        attempt_id = claim.identity.attempt_id
        self._active[attempt_id] = claim
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        finished = False
        try:
            await self.runner.run(claim.identity)
            finished = True
        except asyncio.CancelledError:
            raise
        except DomainError as error:
            await self._settle(claim, error.code)
            finished = True
            raise
        except Exception:
            await self._settle(claim, "analysis_failed_unconfirmed")
            finished = True
            raise
        finally:
            if finished:
                self._active.pop(attempt_id, None)

    async def _dispatch_guarded(self, attempt_id: UUID) -> None:
        try:
            claim = await self.claim(attempt_id)
        except DomainError:
            return
        try:
            await self._run(claim)
        except asyncio.CancelledError:
            raise
        except DomainError:
            return
        except Exception:
            return

    async def _serve(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self._accepting:
                return
            while self._pending and self.accepting:
                try:
                    await self.submit(self._pending.pop())
                except DomainError:
                    self._pending.clear()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            for attempt_id in list(self._active):
                try:
                    await self.heartbeat(attempt_id)
                except StorageUnavailable:
                    return
                except DomainError:
                    continue

    def _ensure_dispatching(self) -> None:
        if self._halted:
            raise StorageUnavailable()
        if not self._accepting:
            raise SupervisorStopped()
        if self.runner is None:
            raise AnalysisRuntimeUnavailable()


@asynccontextmanager
async def supervisor_lifecycle(supervisor: ProposalAttemptSupervisor) -> AsyncIterator[None]:
    await supervisor.start()
    try:
        yield
    finally:
        await supervisor.stop()


__all__ = [
    "AnalysisRuntimeUnavailable",
    "AttemptClaim",
    "AttemptRunner",
    "DrainStatus",
    "ProposalAttemptSupervisor",
    "ScanFinding",
    "SupervisorSettings",
    "SupervisorStopped",
    "supervisor_lifecycle",
]
