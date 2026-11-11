from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.pq import TransactionStatus

from presales.domain.proposal_analysis_attempt import (
    AttemptStage,
    AttemptTerminal,
    FenceConflict,
    UnsupportedSaver,
)
from presales.infrastructure import proposal_checkpoint
from presales.infrastructure.proposal_checkpoint import (
    ProposalCheckpointFence,
    ProposalCheckpointSaver,
    wrap_checkpoint_saver,
)


class _Result:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, rows, events):
        self.rows = list(rows)
        self.events = events
        self.autocommit = True
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    async def execute(self, sql, params=None):
        self.events.append(("guard", sql))
        if "INSERT INTO presales.proposal_checkpoint_receipts" in sql:
            return _Result(None)
        return _Result(self.rows.pop(0))

    def transaction(self):
        return _Transaction(self.events)

    def cursor(self, **kwargs):
        return _CursorContext(self.events)


class _Transaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append(("transaction", "enter"))
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append(("transaction", "exit"))


class _CursorContext:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append(("cursor", "open"))
        return _FakeCursor(self.events)

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append(("cursor", "close"))


class _FakeCursor:
    def __init__(self, events):
        self.events = events

    async def execute(self, sql, params=None):
        self.events.append(("cursor", "execute"))

    async def executemany(self, sql, params):
        self.events.append(("cursor", "executemany"))


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return None


def fence_rows(*, stage="running", generation=1, thread="thread-1", cancelled=None):
    task_id, attempt_id = uuid4(), uuid4()
    return (
        (
            {"attempt_id": attempt_id, "execution_generation": generation},
            {"state": "new"},
            {"attempt_id": attempt_id, "execution_generation": generation},
            {
                "execution_stage": stage,
                "execution_generation": generation,
                "thread_id": thread,
                "task_id": task_id,
                "cancelled_at": cancelled,
                "run_revision": 4,
            },
        ),
        task_id,
        attempt_id,
    )


@pytest.mark.asyncio
async def test_fence_receipt_checks_generation_thread_slots_and_terminal_state():
    rows, task_id, attempt_id = fence_rows()
    events = []
    conn = _FakeConnection(rows, events)
    fence = ProposalCheckpointFence(
        attempt_id=attempt_id,
        task_id=task_id,
        thread_id="thread-1",
        execution_generation=1,
    )
    receipt = await fence.check(conn)
    assert receipt.attempt_id == attempt_id
    assert receipt.execution_generation == 1
    assert receipt.run_revision == 4
    assert len([event for event in events if event[0] == "guard"]) == 4

    rows, task_id, attempt_id = fence_rows(generation=2)
    fence = ProposalCheckpointFence(
        attempt_id=attempt_id,
        task_id=task_id,
        thread_id="thread-1",
        execution_generation=1,
    )
    with pytest.raises(FenceConflict):
        await fence.check(_FakeConnection(rows, []))

    rows, task_id, attempt_id = fence_rows(stage=AttemptStage.FINISHED.value)
    fence = ProposalCheckpointFence(
        attempt_id=attempt_id,
        task_id=task_id,
        thread_id="thread-1",
        execution_generation=1,
    )
    with pytest.raises(AttemptTerminal):
        await fence.check(_FakeConnection(rows, []))

    rows, task_id, attempt_id = fence_rows(cancelled=datetime.now(UTC))
    fence = ProposalCheckpointFence(
        attempt_id=attempt_id,
        task_id=task_id,
        thread_id="thread-1",
        execution_generation=1,
    )
    with pytest.raises(AttemptTerminal):
        await fence.check(_FakeConnection(rows, []))


@pytest.mark.asyncio
async def test_checkpoint_and_pending_writes_run_guard_inside_transaction(monkeypatch):
    rows, task_id, attempt_id = fence_rows()
    events = []
    connection = _FakeConnection(rows * 2, events)
    monkeypatch.setattr(
        proposal_checkpoint,
        "get_connection",
        lambda _: _ConnectionContext(connection),
    )
    base = AsyncPostgresSaver(conn=object())
    fence = ProposalCheckpointFence(
        attempt_id=attempt_id,
        task_id=task_id,
        thread_id="thread-1",
        execution_generation=1,
    )
    saver = wrap_checkpoint_saver(base, fence)
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
        }
    }
    checkpoint = {
        "v": 4,
        "id": "cp-1",
        "ts": datetime.now(UTC).isoformat(),
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    await saver.aput(config, checkpoint, {}, {})
    await saver.aput_writes(config, [("messages", "synthetic")], "task-1")
    transaction_index = events.index(("transaction", "enter"))
    guard_indices = [index for index, event in enumerate(events) if event[0] == "guard"]
    cursor_write_indices = [
        index
        for index, event in enumerate(events)
        if event[0] == "cursor" and event[1] in {"execute", "executemany"}
    ]
    assert transaction_index < min(guard_indices)
    assert min(guard_indices) < min(cursor_write_indices)
    assert saver.last_receipt is not None


@pytest.mark.asyncio
async def test_wrong_thread_delete_and_non_postgres_saver_are_blocked():
    rows, task_id, attempt_id = fence_rows()
    fence = ProposalCheckpointFence(
        attempt_id=attempt_id,
        task_id=task_id,
        thread_id="thread-1",
        execution_generation=1,
    )
    saver = ProposalCheckpointSaver(AsyncPostgresSaver(conn=object()), fence)
    with pytest.raises(FenceConflict):
        saver.fence.require_config({"configurable": {"thread_id": "other"}})
    with pytest.raises(UnsupportedSaver):
        await saver.adelete_thread("thread-1")
    with pytest.raises(UnsupportedSaver):
        wrap_checkpoint_saver(SimpleNamespace(), fence)


@pytest.mark.asyncio
async def test_uncommitted_outer_transaction_cannot_publish_a_receipt(monkeypatch):
    rows, task_id, attempt_id = fence_rows()
    connection = _FakeConnection(rows, [])
    connection.autocommit = False
    monkeypatch.setattr(
        proposal_checkpoint, "get_connection", lambda _: _ConnectionContext(connection)
    )
    saver = wrap_checkpoint_saver(
        AsyncPostgresSaver(conn=object()),
        ProposalCheckpointFence(attempt_id, task_id, "thread-1", 1),
    )
    config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "1"}}
    with pytest.raises(UnsupportedSaver):
        await saver.aput_writes(config, [("messages", "synthetic")], "task-1")
    assert saver.last_receipt is None
    assert connection.events == []
