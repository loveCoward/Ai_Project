"""Registers actually-read sources and node artifacts, then reconciles them on re-entry."""

from collections.abc import Iterable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from presales.application.proposal_attempt_context import ProposalAttemptContext
from presales.domain.contracts import digest
from presales.domain.evidence_snapshot import (
    DerivedArtifact,
    EvidenceContractViolation,
    NodeExecutionIdentity,
    ReconciliationStatus,
    SnapshotMissingSource,
    SnapshotNotReady,
    SourceReference,
)
from presales.infrastructure.evidence_snapshot_store import EvidenceSnapshotStore

OPEN_CALLS_SQL = """
SELECT record_key,status FROM presales.proposal_execution_records
WHERE attempt_id=:attempt AND kind='call' AND status IN ('registered','unknown')
  AND (body->>'node')=:node
ORDER BY record_key
"""


class EvidenceSnapshotService:
    """Persists immutable node outputs with their direct dependency closure."""

    def __init__(self, context: ProposalAttemptContext) -> None:
        self.context = context

    async def save(
        self,
        identity: NodeExecutionIdentity,
        *,
        body: dict[str, Any],
        sources: Iterable[SourceReference] = (),
        upstream: Iterable[UUID] = (),
    ) -> DerivedArtifact:
        collected = tuple(sources)
        parents = tuple(upstream)
        self._require_sources(collected)
        await self._require_known_versions(self._source_versions(collected))
        async with self.context.boundary(self._source_versions(collected)) as (
            conn,
            _attempt,
            _records,
        ):
            store = EvidenceSnapshotStore(conn)
            for parent in parents:
                await self._require_parent(store, identity, parent)
            artifact = await store.save(
                identity,
                artifact_id=uuid4(),
                output_digest=digest(
                    {
                        "body": body,
                        "sources": [source.model_dump(mode="json") for source in collected],
                    }
                ),
                body=body,
                sources=collected,
                upstream=parents,
            )
            await store.assert_closure(artifact.artifact_id)
            await store.set_readiness(
                identity,
                state="ready",
                detail={"artifact_id": str(artifact.artifact_id)},
            )
            return artifact

    async def reconcile(self, identity: NodeExecutionIdentity) -> ReconciliationStatus:
        async with self.context.boundary(()) as (conn, _attempt, _records):
            store = EvidenceSnapshotStore(conn)
            artifact = await store.existing(identity)
            if artifact is not None and artifact.state == "ready":
                await store.assert_closure(artifact.artifact_id)
                return ReconciliationStatus(
                    identity=identity,
                    state="reusable",
                    artifact_id=artifact.artifact_id,
                )
            open_calls = tuple(
                row["record_key"]
                for row in await _rows(
                    conn,
                    OPEN_CALLS_SQL,
                    {"attempt": identity.attempt_id, "node": identity.node_name},
                )
            )
            if open_calls:
                return ReconciliationStatus(
                    identity=identity, state="unknown", open_calls=open_calls
                )
            return ReconciliationStatus(identity=identity, state="not_ready")

    async def load(self, identity: NodeExecutionIdentity) -> DerivedArtifact:
        async with self.context.boundary(()) as (conn, _attempt, _records):
            store = EvidenceSnapshotStore(conn)
            artifact = await store.existing(identity)
            if artifact is None:
                raise SnapshotNotReady()
            await store.assert_closure(artifact.artifact_id)
            return artifact

    async def mark_pending(
        self, identity: NodeExecutionIdentity, *, detail: dict[str, Any] | None = None
    ) -> None:
        async with self.context.boundary(()) as (conn, _attempt, _records):
            await EvidenceSnapshotStore(conn).set_readiness(
                identity, state="pending", detail=detail or {}
            )

    async def link_upstream(
        self, identity: NodeExecutionIdentity, *, artifact_id: UUID, parent: UUID
    ) -> None:
        async with self.context.boundary(()) as (conn, _attempt, _records):
            store = EvidenceSnapshotStore(conn)
            await self._require_parent(store, identity, parent)
            await store.link_upstream(artifact_id, parent)

    async def set_failed(self, identity: NodeExecutionIdentity, *, error_code: str) -> None:
        if not error_code:
            raise EvidenceContractViolation()
        async with self.context.boundary(()) as (conn, _attempt, _records):
            await EvidenceSnapshotStore(conn).set_readiness(
                identity, state="failed", detail={"error_code": error_code}
            )

    async def _require_parent(
        self,
        store: EvidenceSnapshotStore,
        identity: NodeExecutionIdentity,
        parent: UUID,
    ) -> None:
        rows = await _rows(
            store.conn,
            "SELECT attempt_id,state FROM presales.evidence_snapshots WHERE artifact_id=:id",
            {"id": parent},
        )
        if not rows or rows[0]["attempt_id"] != identity.attempt_id or rows[0]["state"] != "ready":
            raise SnapshotMissingSource()

    async def _require_known_versions(self, versions: tuple[UUID, ...]) -> None:
        if not versions:
            return
        async with self.context.database.transaction() as conn:
            known = set()
            for version in versions:
                found = await _rows(
                    conn,
                    "SELECT id FROM presales.resource_versions WHERE id=:id",
                    {"id": version},
                )
                if found:
                    known.add(found[0]["id"])
        if known != set(versions):
            raise SnapshotMissingSource()

    @staticmethod
    def _require_sources(sources: tuple[SourceReference, ...]) -> None:
        seen: set[tuple[str, UUID]] = set()
        for source in sources:
            key = (source.kind, source.source_version_id)
            if key in seen:
                raise EvidenceContractViolation()
            seen.add(key)

    @staticmethod
    def _source_versions(sources: tuple[SourceReference, ...]) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys(source.source_version_id for source in sources))


async def _rows(conn, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = await conn.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


__all__ = ["EvidenceSnapshotService"]
