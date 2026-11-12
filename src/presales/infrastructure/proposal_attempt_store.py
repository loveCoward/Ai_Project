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

READ_TASK_FOR_START_SQL = """
SELECT * FROM presales.tasks WHERE id=:task FOR UPDATE
"""

READ_ACTIVE_ATTEMPT_SQL = """
SELECT * FROM presales.proposal_analysis_attempts
WHERE task_id=:task AND execution_stage<>'finished' FOR UPDATE
"""

READ_TASK_FOR_CANCEL_SQL = """
SELECT * FROM presales.tasks WHERE id=:task FOR UPDATE
"""

READ_ATTEMPT_FOR_CANCEL_SQL = """
SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id FOR UPDATE
"""

READ_TASK_SLOT_FOR_CANCEL_SQL = """
SELECT * FROM presales.proposal_task_slots WHERE task_id=:task FOR UPDATE
"""

READ_GLOBAL_SLOT_FOR_CANCEL_SQL = """
SELECT * FROM presales.proposal_global_slot WHERE singleton=true FOR UPDATE
"""

READ_OPEN_CALLS_SQL = """
SELECT record_key FROM presales.proposal_execution_records
WHERE attempt_id=:attempt AND kind='call' AND status IN ('registered','unknown')
"""

READ_RECEIPT_SQL = """
SELECT parameter_digest,result FROM presales.command_receipts
WHERE actor=:actor AND operation=:operation AND request_id=:request
"""

WRITE_RECEIPT_SQL = """
INSERT INTO presales.command_receipts
(actor,operation,request_id,parameter_digest,result)
VALUES(:actor,:operation,:request,:digest,CAST(:result AS jsonb))
"""

BEGIN_ANALYSIS_SQL = """
SELECT presales.begin_proposal_analysis(:actor,:task)
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

CANCEL_TASK_SQL = """
SELECT presales.cancel_proposal_task(:actor,:task,:expected_revision)
"""

FINISH_CANCELLED_ATTEMPT_SQL = """
UPDATE presales.proposal_analysis_attempts
SET execution_stage='finished',outcome='cancelled',
    run_revision=run_revision+1,terminal_at=now(),error_code='cancelled',
    result_ready=false,result_revision_id=NULL,updated_at=now()
WHERE id=:attempt AND execution_generation=:generation
  AND run_revision=:expected_run_revision
  AND cancelled_at IS NOT NULL AND execution_stage='reconciling'
"""

RESUME_ATTEMPT_SQL = """
SELECT presales.resume_proposal_attempt(
  :actor,:attempt,:generation,:run_revision,:task_revision,:input_revision,:request,
  :lease_seconds)
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

    async def global_slot_for_start(self) -> dict:
        result = await self.conn.execute(
            text("SELECT * FROM presales.proposal_global_slot WHERE singleton=true FOR UPDATE")
        )
        return dict(result.mappings().one())

    async def task_for_start(self, task_id: UUID) -> dict | None:
        result = await self.conn.execute(text(READ_TASK_FOR_START_SQL), {"task": task_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def active_attempt(self, task_id: UUID) -> dict | None:
        result = await self.conn.execute(text(READ_ACTIVE_ATTEMPT_SQL), {"task": task_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def task_for_cancel(self, task_id: UUID) -> dict | None:
        result = await self.conn.execute(text(READ_TASK_FOR_CANCEL_SQL), {"task": task_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def attempt_for_cancel(self, attempt_id: UUID) -> dict | None:
        result = await self.conn.execute(text(READ_ATTEMPT_FOR_CANCEL_SQL), {"id": attempt_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def task_slot_for_cancel(self, task_id: UUID) -> dict | None:
        result = await self.conn.execute(text(READ_TASK_SLOT_FOR_CANCEL_SQL), {"task": task_id})
        row = result.mappings().first()
        return dict(row) if row else None

    async def global_slot_for_cancel(self) -> dict:
        result = await self.conn.execute(text(READ_GLOBAL_SLOT_FOR_CANCEL_SQL))
        return dict(result.mappings().one())

    async def open_calls(self, attempt_id: UUID) -> list[str]:
        result = await self.conn.execute(text(READ_OPEN_CALLS_SQL), {"attempt": attempt_id})
        return [row["record_key"] for row in result.mappings()]

    async def receipt(self, actor: UUID, operation: str, request_id: UUID) -> dict | None:
        result = await self.conn.execute(
            text(READ_RECEIPT_SQL),
            {"actor": actor, "operation": operation, "request": request_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def write_receipt(
        self,
        actor: UUID,
        operation: str,
        request_id: UUID,
        parameter_digest: str,
        result: dict,
    ) -> None:
        await self.conn.execute(
            text(WRITE_RECEIPT_SQL),
            {
                "actor": actor,
                "operation": operation,
                "request": request_id,
                "digest": parameter_digest,
                "result": canonical(result),
            },
        )

    async def begin_task_analysis(self, actor: UUID, task_id: UUID) -> None:
        await self.conn.execute(text(BEGIN_ANALYSIS_SQL), {"actor": actor, "task": task_id})

    async def task_inputs(self, task_id: UUID) -> list[UUID]:
        result = await self.conn.execute(
            text("SELECT material_version_id FROM presales.task_inputs WHERE task_id=:task"),
            {"task": task_id},
        )
        return [row["material_version_id"] for row in result.mappings()]

    async def input_necessity(self, task_id: UUID, revision: int) -> dict[UUID, str]:
        result = await self.conn.execute(
            text(
                """SELECT material_version_id,necessity,input_revision
                FROM presales.task_input_manifest
                WHERE task_id=:task AND input_revision<=:revision"""
            ),
            {"task": task_id, "revision": revision},
        )
        chosen: dict[UUID, tuple[int, str]] = {}
        for row in result.mappings():
            current = chosen.get(row["material_version_id"])
            if current is None or row["input_revision"] > current[0]:
                chosen[row["material_version_id"]] = (row["input_revision"], row["necessity"])
        return {key: value[1] for key, value in chosen.items()}

    async def parsed_size(self, version_id: UUID, parser: str) -> dict | None:
        result = await self.conn.execute(
            text(
                """SELECT j.state,j.id AS job_id,r.character_count
                FROM presales.parse_jobs j
                LEFT JOIN presales.parse_results r ON r.job_id=j.id
                WHERE j.source_version_id=:id AND j.parser_version=:parser"""
            ),
            {"id": version_id, "parser": parser},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def declarations(self, version_ids: tuple[UUID, ...]) -> set[str]:
        if not version_ids:
            return set()
        result = await self.conn.execute(
            text(
                """WITH RECURSIVE deps(id) AS (
                SELECT value::uuid FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) UNION
                SELECT d.upstream_id FROM presales.direct_dependencies d
                JOIN deps ON d.downstream_id=deps.id)
                SELECT DISTINCT declaration FROM presales.resource_versions
                WHERE id IN (SELECT id FROM deps) AND source_kind='original'"""
            ),
            {"ids": canonical([str(item) for item in version_ids])},
        )
        return {row["declaration"] for row in result.mappings()}

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

    async def accept_attempt(
        self,
        attempt: ProposalAnalysisAttempt,
        *,
        actor: UUID,
        owner: UUID,
        lease_until: datetime,
    ) -> ProposalAnalysisAttempt:
        await self.create_attempt(attempt, owner=owner, lease_until=lease_until)
        await self.begin_task_analysis(actor, attempt.task_id)
        await _rowcount(
            self.conn,
            UPDATE_PROJECTION_SQL,
            attempt=attempt.id,
            generation=attempt.execution_generation,
            expected_run_revision=attempt.run_revision,
            stage=AttemptStage.ACCEPTED.value,
            outcome=None,
            result_ready=False,
            result_revision_id=None,
            error_code=None,
        )
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
        await self.release_slots(attempt_id, expected_generation)
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

    async def cancel_task(self, actor: UUID, task_id: UUID, expected_revision: int) -> int:
        result = await self.conn.execute(
            text(CANCEL_TASK_SQL),
            {"actor": actor, "task": task_id, "expected_revision": expected_revision},
        )
        row = result.mappings().one()
        return int(row["cancel_proposal_task"])

    async def finish_cancelled_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_generation: int,
        expected_run_revision: int,
    ) -> RunProjection:
        changed = await _rowcount(
            self.conn,
            FINISH_CANCELLED_ATTEMPT_SQL,
            attempt=attempt_id,
            generation=expected_generation,
            expected_run_revision=expected_run_revision,
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

    async def release_slots(self, attempt_id: UUID, generation: int) -> None:
        await self.conn.execute(
            text(RELEASE_TASK_SLOT_SQL),
            {"attempt": attempt_id, "generation": generation},
        )
        await self.conn.execute(
            text(RELEASE_GLOBAL_SLOT_SQL),
            {"attempt": attempt_id, "generation": generation},
        )

    async def resume_attempt(
        self,
        attempt_id: UUID,
        *,
        actor: UUID,
        expected_generation: int,
        expected_run_revision: int,
        expected_task_revision: int,
        expected_input_revision: int,
        intent_request: UUID,
        lease_seconds: int = 30,
    ) -> int:
        result = await self.conn.execute(
            text(RESUME_ATTEMPT_SQL),
            {
                "actor": actor,
                "attempt": attempt_id,
                "generation": expected_generation,
                "run_revision": expected_run_revision,
                "task_revision": expected_task_revision,
                "input_revision": expected_input_revision,
                "request": intent_request,
                "lease_seconds": lease_seconds,
            },
        )
        return int(result.mappings().one()["resume_proposal_attempt"])

    async def _explain_write_failure(
        self, attempt_id: UUID, generation: int, expected_run_revision: int
    ) -> None:
        row = await self.read(attempt_id)
        if row is None:
            raise AccessDenied()
        if row["execution_generation"] != generation:
            raise FenceConflict()
        if row["execution_stage"] == AttemptStage.FINISHED.value or row["cancelled_at"] is not None:
            raise AttemptTerminal()
        if row["run_revision"] != expected_run_revision:
            raise Conflict()
        raise Conflict()


__all__ = [
    "CANCEL_ATTEMPT_SQL",
    "CANCEL_TASK_SQL",
    "CLAIM_GLOBAL_SLOT_SQL",
    "CLAIM_TASK_SLOT_SQL",
    "COMMIT_RESULT_SQL",
    "CREATE_ATTEMPT_SQL",
    "FINISH_CANCELLED_ATTEMPT_SQL",
    "ProposalAttemptStore",
    "READ_ATTEMPT_FOR_CANCEL_SQL",
    "READ_ATTEMPT_BY_THREAD_SQL",
    "READ_ATTEMPT_SQL",
    "READ_GLOBAL_SLOT_FOR_CANCEL_SQL",
    "READ_OPEN_CALLS_SQL",
    "READ_TASK_FOR_CANCEL_SQL",
    "RESUME_ATTEMPT_SQL",
    "READ_TASK_SLOT_FOR_CANCEL_SQL",
    "RELEASE_GLOBAL_SLOT_SQL",
    "RELEASE_TASK_SLOT_SQL",
    "TRANSITION_SQL",
    "UPDATE_PROJECTION_SQL",
]
