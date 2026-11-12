"""Persists Evidence Snapshot rows, source edges and node readiness under attempt fencing."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from presales.domain.contracts import AccessDenied, Conflict, canonical
from presales.domain.evidence_snapshot import (
    DerivedArtifact,
    EvidenceContractViolation,
    NodeExecutionIdentity,
    SnapshotCycleDetected,
    SnapshotMissingSource,
    SourceReference,
)
from presales.domain.proposal_analysis_attempt import AttemptTerminal, FenceConflict
from presales.infrastructure.proposal_execution_records import ProposalExecutionRecords

READ_ATTEMPT_SQL = """
SELECT execution_generation,execution_stage,cancelled_at,input_revision
FROM presales.proposal_analysis_attempts WHERE id=:id FOR SHARE
"""

READ_ARTIFACT_SQL = """
SELECT * FROM presales.evidence_snapshots
WHERE attempt_id=:attempt AND node_name=:node AND input_revision=:input_revision
  AND reflection_revision=:reflection AND technical_retry=:retry
"""

INSERT_ARTIFACT_SQL = """
INSERT INTO presales.evidence_snapshots
(artifact_id,attempt_id,node_name,input_revision,execution_generation,reflection_revision,
 technical_retry,state,output_digest,body)
VALUES(:artifact_id,:attempt_id,:node,:input_revision,:generation,:reflection,:retry,
       :state,:output_digest,CAST(:body AS jsonb))
ON CONFLICT(attempt_id,node_name,input_revision,reflection_revision,technical_retry) DO NOTHING
"""

INSERT_SOURCE_SQL = """
INSERT INTO presales.evidence_snapshot_sources
(artifact_id,ordinal,kind,source_version_id,evidence_id,locator,content_sha256,
 declaration,applicability,retrieval_run_id)
VALUES(:artifact_id,:ordinal,:kind,:source_version_id,:evidence_id,CAST(:locator AS jsonb),
       :content_sha256,:declaration,CAST(:applicability AS jsonb),:retrieval_run_id)
"""

INSERT_UPSTREAM_SQL = """
INSERT INTO presales.evidence_snapshot_upstream(artifact_id,ordinal,upstream_artifact_id)
VALUES(:artifact_id,:ordinal,:upstream)
"""

UPSERT_READINESS_SQL = """
INSERT INTO presales.node_readiness
(attempt_id,node_key,execution_generation,state,detail)
VALUES(:attempt_id,:node_key,:generation,:state,CAST(:detail AS jsonb))
ON CONFLICT(attempt_id,node_key) DO UPDATE
SET state=EXCLUDED.state,detail=EXCLUDED.detail,updated_at=now()
WHERE presales.node_readiness.execution_generation=EXCLUDED.execution_generation
"""

READ_READINESS_SQL = """
SELECT * FROM presales.node_readiness WHERE attempt_id=:attempt AND node_key=:key
"""

READ_SOURCES_SQL = """
SELECT * FROM presales.evidence_snapshot_sources
WHERE artifact_id=:artifact ORDER BY ordinal
"""

READ_UPSTREAM_SQL = """
SELECT upstream_artifact_id FROM presales.evidence_snapshot_upstream
WHERE artifact_id=:artifact ORDER BY ordinal
"""

ANCESTRY_SQL = """
WITH RECURSIVE ancestors(id) AS (
  SELECT :artifact
  UNION
  SELECT u.upstream_artifact_id FROM presales.evidence_snapshot_upstream u
  JOIN ancestors a ON u.artifact_id=a.id
) SELECT id FROM ancestors
"""


class EvidenceSnapshotStore:
    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn
        self.records = ProposalExecutionRecords(conn)

    async def _attempt(self, attempt_id):
        result = await self.conn.execute(text(READ_ATTEMPT_SQL), {"id": attempt_id})
        attempt = result.mappings().first()
        if attempt is None:
            raise AccessDenied()
        return attempt

    def _fence(self, attempt, identity: NodeExecutionIdentity) -> None:
        if attempt["execution_generation"] != identity.execution_generation:
            raise FenceConflict()
        if attempt["execution_stage"] == "finished" or attempt["cancelled_at"] is not None:
            raise AttemptTerminal()
        if attempt["input_revision"] != identity.input_revision:
            raise Conflict()

    async def existing(self, identity: NodeExecutionIdentity) -> DerivedArtifact | None:
        result = await self.conn.execute(
            text(READ_ARTIFACT_SQL),
            {
                "attempt": identity.attempt_id,
                "node": identity.node_name,
                "input_revision": identity.input_revision,
                "reflection": identity.reflection_revision,
                "retry": identity.technical_retry,
            },
        )
        row = result.mappings().first()
        return await self.hydrate(dict(row)) if row else None

    async def hydrate(self, row: dict[str, Any]) -> DerivedArtifact:
        sources = await self.conn.execute(text(READ_SOURCES_SQL), {"artifact": row["artifact_id"]})
        upstream = await self.conn.execute(
            text(READ_UPSTREAM_SQL), {"artifact": row["artifact_id"]}
        )
        return DerivedArtifact(
            artifact_id=row["artifact_id"],
            identity=NodeExecutionIdentity(
                attempt_id=row["attempt_id"],
                node_name=row["node_name"],
                input_revision=row["input_revision"],
                execution_generation=row["execution_generation"],
                reflection_revision=row["reflection_revision"],
                technical_retry=row["technical_retry"],
            ),
            state=row["state"],
            output_digest=row["output_digest"],
            body=row["body"],
            sources=tuple(
                SourceReference(
                    kind=item["kind"],
                    source_version_id=item["source_version_id"],
                    evidence_id=item["evidence_id"],
                    locator=item["locator"],
                    content_sha256=item["content_sha256"],
                    declaration=item["declaration"],
                    applicability=item["applicability"],
                    retrieval_run_id=item["retrieval_run_id"],
                )
                for item in sources.mappings()
            ),
            upstream=tuple(item["upstream_artifact_id"] for item in upstream.mappings()),
            created_at=row["created_at"],
        )

    async def save(
        self,
        identity: NodeExecutionIdentity,
        *,
        artifact_id,
        output_digest: str,
        body: dict[str, Any],
        sources: tuple[SourceReference, ...],
        upstream: tuple,
    ) -> DerivedArtifact:
        attempt = await self._attempt(identity.attempt_id)
        self._fence(attempt, identity)
        existing = await self.existing(identity)
        if existing is not None:
            if existing.output_digest != output_digest or existing.body != body:
                raise Conflict()
            return existing
        await self.conn.execute(
            text(INSERT_ARTIFACT_SQL),
            {
                "artifact_id": artifact_id,
                "attempt_id": identity.attempt_id,
                "node": identity.node_name,
                "input_revision": identity.input_revision,
                "generation": identity.execution_generation,
                "reflection": identity.reflection_revision,
                "retry": identity.technical_retry,
                "state": "ready",
                "output_digest": output_digest,
                "body": canonical(body),
            },
        )
        for ordinal, source in enumerate(sources):
            await self._insert_source(artifact_id, ordinal, source)
        for ordinal, parent in enumerate(upstream):
            await self._insert_upstream(artifact_id, ordinal, parent)
        created = await self.existing(identity)
        if created is None:
            raise Conflict()
        return created

    async def _insert_source(self, artifact_id, ordinal: int, source: SourceReference) -> None:
        exists = await self.conn.execute(
            text("SELECT 1 FROM presales.resource_versions WHERE id=:id"),
            {"id": source.source_version_id},
        )
        if exists.first() is None:
            raise SnapshotMissingSource()
        await self.conn.execute(
            text(INSERT_SOURCE_SQL),
            {
                "artifact_id": artifact_id,
                "ordinal": ordinal,
                "kind": source.kind,
                "source_version_id": source.source_version_id,
                "evidence_id": source.evidence_id,
                "locator": canonical(source.locator),
                "content_sha256": source.content_sha256,
                "declaration": source.declaration,
                "applicability": canonical(source.applicability),
                "retrieval_run_id": source.retrieval_run_id,
            },
        )

    async def _insert_upstream(self, artifact_id, ordinal: int, parent) -> None:
        exists = await self.conn.execute(
            text("SELECT 1 FROM presales.evidence_snapshots WHERE artifact_id=:id"),
            {"id": parent},
        )
        if exists.first() is None:
            raise SnapshotMissingSource()
        cycle = await self.conn.execute(text(ANCESTRY_SQL), {"artifact": parent})
        if any(item["id"] == artifact_id for item in cycle.mappings()):
            raise SnapshotCycleDetected()
        await self.conn.execute(
            text(INSERT_UPSTREAM_SQL),
            {"artifact_id": artifact_id, "ordinal": ordinal, "upstream": parent},
        )

    async def link_upstream(self, artifact_id, parent) -> None:
        """Adds a direct dependency edge to an existing artifact; the cycle guard still applies."""
        result = await self.conn.execute(
            text("SELECT 1 FROM presales.evidence_snapshots WHERE artifact_id=:id"),
            {"id": artifact_id},
        )
        if result.first() is None:
            raise SnapshotMissingSource()
        next_ordinal = await self.conn.execute(
            text(
                "SELECT coalesce(max(ordinal),-1)+1 AS next FROM "
                "presales.evidence_snapshot_upstream WHERE artifact_id=:id"
            ),
            {"id": artifact_id},
        )
        await self._insert_upstream(artifact_id, int(next_ordinal.scalar_one()), parent)

    async def assert_closure(self, artifact_id) -> None:
        result = await self.conn.execute(
            text("SELECT * FROM presales.evidence_snapshots WHERE artifact_id=:id"),
            {"id": artifact_id},
        )
        if result.mappings().first() is None:
            raise EvidenceContractViolation()
        sources = await self.conn.execute(text(READ_SOURCES_SQL), {"artifact": artifact_id})
        for item in sources.mappings():
            version = await self.conn.execute(
                text("SELECT * FROM presales.resource_versions WHERE id=:id"),
                {"id": item["source_version_id"]},
            )
            value = version.mappings().first()
            if value is None or not value["ready"] or not value["dependencies_complete"]:
                raise EvidenceContractViolation()

    async def set_readiness(
        self,
        identity: NodeExecutionIdentity,
        *,
        state: str,
        detail: dict[str, Any],
    ) -> None:
        attempt = await self._attempt(identity.attempt_id)
        self._fence(attempt, identity)
        await self.conn.execute(
            text(UPSERT_READINESS_SQL),
            {
                "attempt_id": identity.attempt_id,
                "node_key": identity.key,
                "generation": identity.execution_generation,
                "state": state,
                "detail": canonical(detail),
            },
        )

    async def readiness(self, identity: NodeExecutionIdentity) -> dict[str, Any] | None:
        result = await self.conn.execute(
            text(READ_READINESS_SQL),
            {"attempt": identity.attempt_id, "key": identity.key},
        )
        row = result.mappings().first()
        return dict(row) if row else None


__all__ = ["EvidenceSnapshotStore"]
