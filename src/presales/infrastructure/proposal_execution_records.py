"""Attempt-scoped call, recovery and node-result persistence without execution policy."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from presales.domain.contracts import AccessDenied, Conflict, canonical
from presales.domain.proposal_analysis_attempt import AttemptTerminal, FenceConflict


class ProposalExecutionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    kind: Literal["call", "recovery", "node_result"]
    record_key: str = Field(min_length=1, max_length=200)
    execution_generation: int = Field(ge=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["registered", "unknown", "completed", "failed"]
    body: dict[str, Any]
    result_revision_id: UUID | None = None


class ProposalExecutionRecords:
    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn

    async def save(self, record: ProposalExecutionRecord) -> ProposalExecutionRecord:
        result = await self.conn.execute(
            text("SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id FOR SHARE"),
            {"id": record.attempt_id},
        )
        attempt = result.mappings().first()
        if attempt is None:
            raise AccessDenied()
        if attempt["execution_generation"] != record.execution_generation:
            raise FenceConflict()
        if attempt["execution_stage"] == "finished" or attempt["cancelled_at"] is not None:
            raise AttemptTerminal()
        parameters = record.model_dump()
        parameters["body"] = canonical(record.body)
        await self.conn.execute(
            text("""INSERT INTO presales.proposal_execution_records
            (attempt_id,kind,record_key,execution_generation,input_digest,status,body,result_revision_id)
            VALUES(:attempt_id,:kind,:record_key,:execution_generation,:input_digest,:status,
                   CAST(:body AS jsonb),:result_revision_id)
            ON CONFLICT(attempt_id,kind,record_key) DO NOTHING"""),
            parameters,
        )
        existing = await self.read(record.attempt_id, record.kind, record.record_key)
        if existing is None or existing != record:
            raise Conflict()
        return existing

    async def read(
        self, attempt_id: UUID, kind: str, record_key: str
    ) -> ProposalExecutionRecord | None:
        result = await self.conn.execute(
            text("""SELECT attempt_id,kind,record_key,execution_generation,input_digest,
                    status,body,result_revision_id FROM presales.proposal_execution_records
                    WHERE attempt_id=:attempt AND kind=:kind AND record_key=:key"""),
            {"attempt": attempt_id, "kind": kind, "key": record_key},
        )
        row = result.mappings().first()
        return ProposalExecutionRecord.model_validate(dict(row)) if row else None

    async def complete(
        self,
        record: ProposalExecutionRecord,
        *,
        status: Literal["unknown", "completed", "failed"],
        body: dict[str, Any],
        result_revision_id: UUID | None = None,
    ) -> ProposalExecutionRecord:
        result = await self.conn.execute(
            text("SELECT * FROM presales.proposal_analysis_attempts WHERE id=:id FOR SHARE"),
            {"id": record.attempt_id},
        )
        attempt = result.mappings().first()
        if attempt is None:
            raise AccessDenied()
        if attempt["execution_generation"] != record.execution_generation:
            raise FenceConflict()
        if attempt["execution_stage"] == "finished" or attempt["cancelled_at"] is not None:
            raise AttemptTerminal()
        updated = await self.conn.execute(
            text("""UPDATE presales.proposal_execution_records
            SET status=:status,body=CAST(:body AS jsonb),result_revision_id=:revision,updated_at=now()
            WHERE attempt_id=:attempt AND kind=:kind AND record_key=:key
              AND execution_generation=:generation AND input_digest=:digest
              AND status IN ('registered','unknown')"""),
            {
                "attempt": record.attempt_id,
                "kind": record.kind,
                "key": record.record_key,
                "generation": record.execution_generation,
                "digest": record.input_digest,
                "status": status,
                "body": canonical(body),
                "revision": result_revision_id,
            },
        )
        expected = record.model_copy(
            update={"status": status, "body": body, "result_revision_id": result_revision_id}
        )
        existing = await self.read(record.attempt_id, record.kind, record.record_key)
        if updated.rowcount != 1 and existing != expected:
            raise Conflict()
        if existing is None:
            raise Conflict()
        return existing
