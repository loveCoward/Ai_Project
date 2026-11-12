"""Evidence Snapshot and derived artifact contracts with stable node execution identity."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from presales.domain.contracts import DomainError


class EvidenceContractViolation(DomainError):
    code = "evidence_contract_failed"


class SnapshotCycleDetected(DomainError):
    code = "cycle_detected"


class SnapshotMissingSource(DomainError):
    code = "missing_source"


class SnapshotNotReady(DomainError):
    code = "not_ready"


class SnapshotSourceInactive(DomainError):
    code = "source_inactive"


SourceKind = Literal["material", "mcp_background", "retrieval", "reflection"]
ArtifactState = Literal["pending", "ready", "failed", "superseded"]


class NodeExecutionIdentity(BaseModel):
    """Stable identity for a node result; technical retries keep this identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    node_name: str = Field(min_length=1, max_length=200)
    input_revision: int = Field(ge=1)
    execution_generation: int = Field(ge=1)
    reflection_revision: int = Field(default=0, ge=0, le=1)
    technical_retry: int = Field(default=0, ge=0, le=2)

    @property
    def key(self) -> str:
        return (
            f"{self.node_name}:{self.input_revision}:{self.reflection_revision}:"
            f"{self.technical_retry}"
        )


class SourceReference(BaseModel):
    """One actually-read source version, with locator and content digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SourceKind
    source_version_id: UUID
    evidence_id: UUID | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    declaration: Literal["synthetic", "owned", "public_authorized"]
    applicability: dict[str, Any] = Field(default_factory=dict)
    retrieval_run_id: UUID | None = None


class DerivedArtifact(BaseModel):
    """Immutable node output plus its directly-read dependencies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    identity: NodeExecutionIdentity
    state: ArtifactState
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    body: dict[str, Any]
    sources: tuple[SourceReference, ...] = ()
    upstream: tuple[UUID, ...] = ()
    error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "DerivedArtifact":
        if self.state in {"ready", "superseded"} and not self.sources and not self.upstream:
            raise ValueError("a completed artifact requires a registered source or upstream")
        if self.state == "failed" and not self.error_code:
            raise ValueError("a failed artifact requires an error code")
        if self.state != "failed" and self.error_code is not None:
            raise ValueError("only failed artifacts carry an error code")
        return self

    def require_ready(self) -> "DerivedArtifact":
        if self.state != "ready":
            raise SnapshotNotReady()
        return self


class ReconciliationStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: NodeExecutionIdentity
    state: Literal["reusable", "not_ready", "fenced", "unknown"]
    artifact_id: UUID | None = None
    open_calls: tuple[str, ...] = ()


__all__ = [
    "ArtifactState",
    "DerivedArtifact",
    "EvidenceContractViolation",
    "NodeExecutionIdentity",
    "ReconciliationStatus",
    "SnapshotCycleDetected",
    "SnapshotMissingSource",
    "SnapshotNotReady",
    "SnapshotSourceInactive",
    "SourceKind",
    "SourceReference",
]
