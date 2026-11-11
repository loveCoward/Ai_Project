"""Fenced checkpoint adapter for an Attempt-owned LangGraph thread."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres._ainternal import get_connection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow, dict_row

from presales.domain.contracts import AccessDenied
from presales.domain.proposal_analysis_attempt import (
    AttemptStage,
    AttemptTerminal,
    FenceConflict,
    UnsupportedSaver,
)

CHECKPOINT_FENCE_SQL = """
SELECT a.execution_stage,a.execution_generation,a.thread_id,a.task_id,
       a.cancelled_at,a.run_revision
FROM presales.proposal_analysis_attempts a
WHERE a.id=%s
FOR SHARE OF a
"""

TASK_SLOT_FENCE_SQL = """
SELECT attempt_id,execution_generation
FROM presales.proposal_task_slots
WHERE task_id=%s AND attempt_id=%s
FOR SHARE
"""

GLOBAL_SLOT_FENCE_SQL = """
SELECT attempt_id,execution_generation
FROM presales.proposal_global_slot
WHERE singleton=true
FOR SHARE
"""


@dataclass(frozen=True)
class CheckpointFenceReceipt:
    attempt_id: UUID
    execution_generation: int
    run_revision: int
    checked_at: datetime


@dataclass(frozen=True)
class ProposalCheckpointFence:
    attempt_id: UUID
    task_id: UUID
    thread_id: str
    execution_generation: int

    def require_config(self, config: RunnableConfig) -> None:
        configurable = config.get("configurable") or {}
        if configurable.get("thread_id") != self.thread_id:
            raise FenceConflict()

    async def check(self, conn: AsyncConnection[DictRow]) -> CheckpointFenceReceipt:
        global_slot = await conn.execute(GLOBAL_SLOT_FENCE_SQL)
        global_row = await global_slot.fetchone()
        if (
            global_row is None
            or global_row["attempt_id"] != self.attempt_id
            or int(global_row["execution_generation"]) != self.execution_generation
        ):
            raise FenceConflict()

        task_state = await conn.execute(
            "SELECT state FROM presales.tasks WHERE id=%s FOR SHARE", (self.task_id,)
        )
        task = await task_state.fetchone()
        if task is None:
            raise AccessDenied()
        if task["state"] == "cancelled":
            raise AttemptTerminal()

        task_slot = await conn.execute(TASK_SLOT_FENCE_SQL, (self.task_id, self.attempt_id))
        task_row = await task_slot.fetchone()
        if task_row is None or int(task_row["execution_generation"]) != self.execution_generation:
            raise FenceConflict()

        cursor = await conn.execute(CHECKPOINT_FENCE_SQL, (self.attempt_id,))
        row = await cursor.fetchone()
        if row is None:
            raise AccessDenied()
        if row["thread_id"] != self.thread_id or row["task_id"] != self.task_id:
            raise FenceConflict()
        if int(row["execution_generation"]) != self.execution_generation:
            raise FenceConflict()
        if row["execution_stage"] == AttemptStage.FINISHED.value:
            raise AttemptTerminal()
        if row["cancelled_at"] is not None:
            raise AttemptTerminal()
        return CheckpointFenceReceipt(
            attempt_id=self.attempt_id,
            execution_generation=self.execution_generation,
            run_revision=int(row["run_revision"]),
            checked_at=datetime.now(UTC),
        )


_ACTIVE_FENCE: ContextVar[ProposalCheckpointFence | None] = ContextVar(
    "presales_active_proposal_checkpoint_fence", default=None
)
_ACTIVE_WRITE: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "presales_active_proposal_checkpoint_write", default=None
)


class ProposalCheckpointSaver(AsyncPostgresSaver):
    def __init__(self, saver: AsyncPostgresSaver, fence: ProposalCheckpointFence) -> None:
        if not isinstance(saver, AsyncPostgresSaver) or saver.pipe is not None:
            raise UnsupportedSaver()
        super().__init__(conn=saver.conn, pipe=None, serde=saver.serde)
        self.fence = fence
        self.last_receipt: CheckpointFenceReceipt | None = None

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncIterator[Any]:
        fence = _ACTIVE_FENCE.get()
        if fence is None:
            async with super()._cursor(pipeline=pipeline) as cursor:
                yield cursor
            return
        async with self.lock:
            async with get_connection(self.conn) as conn:
                self.last_receipt = None
                if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
                    raise UnsupportedSaver()
                async with conn.transaction():
                    receipt = await fence.check(conn)
                    async with conn.cursor(binary=True, row_factory=dict_row) as cursor:
                        yield cursor
                    operation = _ACTIVE_WRITE.get()
                    if operation is None:
                        raise UnsupportedSaver()
                    await conn.execute(
                        """INSERT INTO presales.proposal_checkpoint_receipts
                        (attempt_id,execution_generation,run_revision,operation,checkpoint_id,
                         write_task_id,checked_at) VALUES(%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING""",
                        (
                            receipt.attempt_id,
                            receipt.execution_generation,
                            receipt.run_revision,
                            *operation,
                            receipt.checked_at,
                        ),
                    )
                self.last_receipt = receipt

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self.fence.require_config(config)
        token = _ACTIVE_FENCE.set(self.fence)
        write_token = _ACTIVE_WRITE.set(("checkpoint", checkpoint["id"], ""))
        try:
            return await super().aput(config, checkpoint, metadata, new_versions)
        finally:
            _ACTIVE_WRITE.reset(write_token)
            _ACTIVE_FENCE.reset(token)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.fence.require_config(config)
        token = _ACTIVE_FENCE.set(self.fence)
        write_token = _ACTIVE_WRITE.set(
            ("pending_writes", config["configurable"]["checkpoint_id"], task_id)
        )
        try:
            return await super().aput_writes(config, writes, task_id, task_path)
        finally:
            _ACTIVE_WRITE.reset(write_token)
            _ACTIVE_FENCE.reset(token)

    async def adelete_thread(self, thread_id: str) -> None:
        raise UnsupportedSaver()


def wrap_checkpoint_saver(
    saver: AsyncPostgresSaver, fence: ProposalCheckpointFence
) -> ProposalCheckpointSaver:
    if not isinstance(saver, AsyncPostgresSaver):
        raise UnsupportedSaver()
    return ProposalCheckpointSaver(saver, fence)


__all__ = [
    "CHECKPOINT_FENCE_SQL",
    "CheckpointFenceReceipt",
    "GLOBAL_SLOT_FENCE_SQL",
    "ProposalCheckpointFence",
    "ProposalCheckpointSaver",
    "TASK_SLOT_FENCE_SQL",
    "wrap_checkpoint_saver",
]
