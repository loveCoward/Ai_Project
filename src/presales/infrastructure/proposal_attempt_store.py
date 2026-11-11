"""Short-transaction persistence for Proposal Analysis Attempts and their slots."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from presales.domain.contracts import AccessDenied, Conflict, canonical
from presales.domain.proposal_analysis_attempt import (
    AttemptBusy,
    AttemptOutcome,
    AttemptStage,
    AttemptTerminal,
    FenceConflict,
    GlobalBusy,
    ProposalAnalysisAttempt,
    RunProjection,
)

CREATE_ATTEMPT_SQL = """
INSERT INTO presales.proposal_analysis_attempts
(id,task_id,actor,thread_id,input_revision,start_scope,model_mode,config_identity,
 execution_generation,execution_stage,outcome,run_revision,budget,reflection_revision)
VALUES(:id,:task,:actor,:thread,:input_revision,CAST(:start_scope AS jsonb),:model_mode,
       :config,:generation,:stage,NULL,:run_revision,CAST(:budget AS jsonb),:reflection)
"""

CLAIM_TASK_SLOT_SQL = """
INSERT INTO presales.proposal_task_slots
(task_id,attempt_id,execution_generation,owner,lease_until)
VALUES(:task,:attempt,:generation,:owner,:lease_until)
ON CONFLICT (task_id) DO NOTHING
"""

CLAIM_GLOBAL_SLOT_SQL = """
INSERT INTO presales.proposal_global_slot
(singleton,attempt_id,execution_generation,owner,lease_until)
VALUES(true,:attempt,:generation,:owner,:lease_until)
ON CONFLICT (singleton) DO UPDATE
SET attempt_id=EXCLUDED.attempt_id,execution_generation=EXCLUDED.execution_generation,
    owner=EXCLUDED.owner,lease_until=EXCLUDED.lease_until
WHERE presales.proposal_global_slot.attempt_id IS NULL OR (
  presales.proposal_global_slot.attempt_id=EXCLUDED.attempt_id
  AND presales.proposal_global_slot.execution_generation=EXCLUDED.execution_generation
)
"""

READ_ATTEMPT_SQL = """
SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id
"""

READ_ATTEMPT_BY_THREAD_SQL = """
SELECT * FROM presales.proposal_analysis_attempts WHERE thread_id=:thread
"""

UPDATE_PROJECTION_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage=:stage,outcome=:outcome,run_revision=run_revision+1,
    result_ready=:result_ready,result_revision_id=:result_revision_id,
    terminal_at=CASE WHEN :stage='finished' THEN now() ELSE NULL END,
    error_code=:error_code,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision
  AND execution_stage<>'finished'
"""

TRANSITION_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage=:stage,outcome=:outcome,run_revision=run_revision+1,
    terminal_at=CASE WHEN :stage='finished' THEN now() ELSE NULL END,
    error_code=:error_code,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision
  AND execution_stage=:from_stage
  AND execution_stage<>'finished'
"""

COMMIT_RESULT_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage='finished',outcome='succeeded',run_revision=run_revision+1,
    result_ready=true,result_revision_id=:result_revision,
    candidate_version_id=:candidate_version,source_snapshot=CAST(:source_snapshot AS jsonb),
    terminal_at=now(),error_code=NULL,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision
  AND execution_stage IN ('running','reconciling')
  AND cancelled_at IS NULL
  AND result_ready=false
"""

CANCEL_ATTEMPT_SQL = """
UPDATE presales.proposal_analysis_attempts
SET cancelled_at=coalesce(cancelled_at,now()),execution_stage='reconciling',
    run_revision=run_revision+1,error_code='cancelled',updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision
  AND execution_stage<>'finished'
"""

RELEASE_TASK_SLOT_SQL = """
DELETE FROM presales.proposal_task_slots
WHERE attempt_id=:attempt AND execution_generation=:generation
"""

RELEASE_GLOBAL_SLOT_SQL = """
UPDATE presales.proposal_global_slot
SET attempt_id=NULL,execution_generation=NULL,owner=NULL,lease_until=NULL
WHERE attempt_id=:attempt AND execution_generation=:generation
"""


async def _rowcount(conn: AsyncConnection, sql: str, **params) -> int:
    result = await conn.execute(text(sql), params)
    return result.rowcount or 0


class ProposalAttemptStore:
    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn

    async def create_attempt(
        self,
        attempt: ProposalAnalysisAttempt,
        *,
        owner: UUID,
        lease_until: datetime,
    ) -> ProposalAnalysisAttempt:
        try:
            await self.conn.execute(
                text(CREATE_ATTEMPT_SQL),
                {
                    "id": attempt.id,
                    "task": attempt.task_id,
                    "actor": attempt.actor_id,
                    "thread": attempt.thread_id,
                    "input_revision": attempt.input_revision,
                    "start_scope": canonical([str(item) for item in attempt.start_scope]),
                    "model_mode": attempt.model_mode.value,
                    "config": attempt.config_identity,
                    "generation": attempt.execution_generation,
                    "stage": attempt.stage.value,
                    "run_revision": attempt.run_revision,
                    "budget": canonical(attempt.budget.model_dump(mode="json")),
                    "reflection": attempt.reflection_revision,
                },
            )
        except IntegrityError:
            raise AttemptBusy() from None
        task_slot = await _rowcount(
            self.conn,
            CLAIM_TASK_SLOT_SQL,
            task=attempt.task_id,
            attempt=attempt.id,
            generation=attempt.execution_generation,
            owner=owner,
            lease_until=lease_until,
        )
        if task_slot != 1:
            raise AttemptBusy()
        global_slot = await _rowcount(
            self.conn,
            CLAIM_GLOBAL_SLOT_SQL,
            attempt=attempt.id,
            generation=attempt.execution_generation,
            owner=owner,
            lease_until=lease_until,
        )
        if global_slot != 1:
            raise GlobalBusy()
        return attempt

    async def read(self, attempt_id: UUID) -> dict | None:
        result = await self.conn.execute(text(READ_ATTEMPT_SQL), {"id": attempt_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def by_thread(self, thread_id: str) -> dict | None:
        result = await self.conn.execute(text(READ_ATTEMPT_BY_THREAD_SQL), {"thread": thread_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def task_slot(self, task_id: UUID) -> dict | None:
        result = await self.conn.execute(
            text("SELECT * FROM presales.proposal_task_slots WHERE task_id=:task"),
            {"task": task_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def global_slot(self) -> dict:
        result = await self.conn.execute(
            text("SELECT * FROM presales.proposal_global_slot WHERE singleton=true")
        )
        return dict(result.mappings().one())

    async def projection(
        self,
        attempt_id: UUID,
        *,
        stage: AttemptStage,
        outcome: AttemptOutcome | None,
        expected_generation: int,
        expected_run_revision: int,
        result_ready: bool,
        result_revision_id: UUID | None,
        error_code: str | None,
    ) -> RunProjection:
        changed = await _rowcount(
            self.conn,
            UPDATE_PROJECTION_SQL,
            attempt=attempt_id,
            generation=expected_generation,
            expected_run_revision=expected_run_revision,
            stage=stage.value,
            outcome=outcome.value if outcome else None,
            result_ready=result_ready,
            result_revision_id=result_revision_id,
            error_code=error_code,
        )
        if changed != 1:
            await self._explain_write_failure(
                attempt_id, expected_generation, expected_run_revision
            )
        row = await self.read(attempt_id)
        if row is None:
            raise AccessDenied()
        return RunProjection(
            attempt_id=attempt_id,
            run_revision=row["run_revision"],
            stage=AttemptStage(row["execution_stage"]),
            outcome=AttemptOutcome(row["outcome"]) if row["outcome"] else None,
            error_code=row["error_code"],
            result_ready=row["result_ready"],
            result_revision_id=row["result_revision_id"],
            terminal_at=row["terminal_at"],
        )

    async def transition(
        self,
        attempt_id: UUID,
        *,
        from_stage: AttemptStage,
        stage: AttemptStage,
        outcome: AttemptOutcome | None,
        expected_generation: int,
        expected_run_revision: int,
        error_code: str | None = None,
    ) -> RunProjection:
        changed = await _rowcount(
            self.conn,
            TRANSITION_SQL,
            attempt=attempt_id,
            generation=expected_generation,
            expected_run_revision=expected_run_revision,
            from_stage=from_stage.value,
            stage=stage.value,
            outcome=outcome.value if outcome else None,
            error_code=error_code,
        )
        if changed != 1:
            await self._explain_write_failure(
                attempt_id, expected_generation, expected_run_revision
            )
        row = await self.read(attempt_id)
        if row is None:
            raise AccessDenied()
        return RunProjection(
            attempt_id=attempt_id,
            run_revision=row["run_revision"],
            stage=AttemptStage(row["execution_stage"]),
            outcome=AttemptOutcome(row["outcome"]) if row["outcome"] else None,
            error_code=row["error_code"],
            result_ready=row["result_ready"],
            result_revision_id=row["result_revision_id"],
            terminal_at=row["terminal_at"],
        )

    async def commit_result(
        self,
        attempt_id: UUID,
        *,
        expected_generation: int,
        expected_run_revision: int,
        result_revision_id: UUID,
        candidate_version_id: UUID,
        source_snapshot: list[UUID],
    ) -> RunProjection:
        changed = await _rowcount(
            self.conn,
            COMMIT_RESULT_SQL,
            attempt=attempt_id,
            generation=expected_generation,
            expected_run_revision=expected_run_revision,
            result_revision=result_revision_id,
            candidate_version=candidate_version_id,
            source_snapshot=canonical([str(item) for item in source_snapshot]),
        )
        if changed != 1:
            await self._explain_write_failure(
                attempt_id, expected_generation, expected_run_revision
            )
        row = await self.read(attempt_id)
        if row is None:
            raise AccessDenied()
        return RunProjection(
            attempt_id=attempt_id,
            run_revision=row["run_revision"],
            stage=AttemptStage(row["execution_stage"]),
            outcome=AttemptOutcome(row["outcome"]) if row["outcome"] else None,
            error_code=row["error_code"],
            result_ready=row["result_ready"],
            result_revision_id=row["result_revision_id"],
            terminal_at=row["terminal_at"],
        )

    async def mark_cancelled(
        self,
        attempt_id: UUID,
        *,
        expected_generation: int,
        expected_run_revision: int,
    ) -> None:
        changed = await _rowcount(
            self.conn,
            CANCEL_ATTEMPT_SQL,
            attempt=attempt_id,
            generation=expected_generation,
            expected_run_revision=expected_run_revision,
        )
        if changed != 1:
            await self._explain_write_failure(
                attempt_id, expected_generation, expected_run_revision
            )

    async def release_slots(self, attempt_id: UUID, generation: int) -> None:
        await self.conn.execute(
            text(RELEASE_TASK_SLOT_SQL),
            {"attempt": attempt_id, "generation": generation},
        )
        await self.conn.execute(
            text(RELEASE_GLOBAL_SLOT_SQL),
            {"attempt": attempt_id, "generation": generation},
        )

    async def _explain_write_failure(
        self, attempt_id: UUID, generation: int, expected_run_revision: int
    ) -> None:
        row = await self.read(attempt_id)
        if row is None:
            raise AccessDenied()
        if row["execution_generation"] != generation:
            raise FenceConflict()
        if row["execution_stage"] == AttemptStage.FINISHED.value:
            raise AttemptTerminal()
        if row["run_revision"] != expected_run_revision:
            raise Conflict()
        raise Conflict()


__all__ = [
    "CANCEL_ATTEMPT_SQL",
    "CLAIM_GLOBAL_SLOT_SQL",
    "CLAIM_TASK_SLOT_SQL",
    "COMMIT_RESULT_SQL",
    "CREATE_ATTEMPT_SQL",
    "ProposalAttemptStore",
    "READ_ATTEMPT_BY_THREAD_SQL",
    "READ_ATTEMPT_SQL",
    "RELEASE_GLOBAL_SLOT_SQL",
    "RELEASE_TASK_SLOT_SQL",
    "TRANSITION_SQL",
    "UPDATE_PROJECTION_SQL",
]
