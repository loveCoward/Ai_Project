"""Proposal Package Schema 1.0 aggregation and legacy manual-draft compatibility."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from presales.domain.contracts import DraftContent, canonical
from presales.domain.proposal_package_sections import (
    Applicability,
    ClarificationStatus,
    CommitmentKind,
    CoverageBlock,
    CoverageCounts,
    CoverageStatus,
    EvidenceItem,
    EvidenceLocation,
    EvidenceSourceType,
    InputReference,
    MemoryStatus,
    NodeStatus,
    PackageAccess,
    PackageIdentity,
    Provenance,
    ReflectionStatus,
    RequirementImportance,
    RequirementItem,
    RequirementScope,
    RiskCategory,
    RiskDisposition,
    RiskSeverity,
)


class DecisionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID
    decided_by: UUID
    decided_at: datetime
    action_scope: str = Field(min_length=1, max_length=1000)


class RiskItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    category: RiskCategory
    severity: RiskSeverity
    basis: str = Field(min_length=1, max_length=10_000)
    impact: str = Field(min_length=1, max_length=10_000)
    mitigation: str = Field(min_length=1, max_length=10_000)
    owner: str | None = Field(default=None, max_length=200)
    disposition: RiskDisposition = RiskDisposition.PENDING
    evidence_ids: tuple[UUID, ...] = ()
    decision: DecisionReference | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> RiskItem:
        if self.disposition == RiskDisposition.ACCEPTED:
            if self.decision is None:
                raise ValueError("accepted risks require an existing decision")
            if self.severity == RiskSeverity.SEVERE:
                raise ValueError("severe risks cannot be accepted")
        return self


class ConfirmationReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmed_by: UUID
    confirmed_at: datetime
    evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)


class ClarificationItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=20_000)
    requirement_ids: tuple[str, ...] = ()
    critical: bool
    status: ClarificationStatus = ClarificationStatus.OPEN
    answer: str | None = Field(default=None, max_length=20_000)
    answer_evidence_ids: tuple[UUID, ...] = ()
    confirmation: ConfirmationReference | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> ClarificationItem:
        if self.status in {ClarificationStatus.ANSWERED, ClarificationStatus.RESOLVED}:
            if not self.answer:
                raise ValueError("answered clarifications require an answer")
        if self.status == ClarificationStatus.RESOLVED:
            if not self.answer_evidence_ids or self.confirmation is None:
                raise ValueError("resolved clarifications require sourced confirmation")
        if self.status in {ClarificationStatus.OPEN, ClarificationStatus.SUPERSEDED}:
            if self.answer is not None:
                raise ValueError("open or superseded clarifications cannot have an answer")
        return self


class Commitment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CommitmentKind
    text: str = Field(min_length=1, max_length=10_000)
    binding: bool = False
    confirmation_required: bool = True

    @model_validator(mode="after")
    def require_non_binding(self) -> Commitment:
        if self.binding or not self.confirmation_required:
            raise ValueError("analysis commitments must be non-binding and unconfirmed")
        return self


class SolutionBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recommended: str = Field(min_length=1, max_length=100_000)
    deployment_integration: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    delivery_notes: tuple[str, ...] = ()
    commitments: tuple[Commitment, ...] = ()


class ReviewBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["not_submitted"] = "not_submitted"
    records: tuple[dict[str, Any], ...] = ()

    @model_validator(mode="after")
    def require_unsubmitted(self) -> ReviewBlock:
        if self.records:
            raise ValueError("analysis output cannot contain review records")
        return self


class ApprovalBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["not_decided"] = "not_decided"
    records: tuple[dict[str, Any], ...] = ()
    approved_action_set: None = None

    @model_validator(mode="after")
    def require_no_decision(self) -> ApprovalBlock:
        if self.records or self.approved_action_set is not None:
            raise ValueError("analysis output cannot contain approval records")
        return self


class ExecutionNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    status: NodeStatus
    output_revision_id: UUID | None = None
    error_code: str | None = Field(default=None, max_length=100)
    degradation: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_node_result(self) -> ExecutionNode:
        if self.status == NodeStatus.SUCCEEDED and self.error_code is not None:
            raise ValueError("successful nodes cannot carry an error")
        if self.status == NodeStatus.FAILED and not self.error_code:
            raise ValueError("failed nodes require an error code")
        return self


class ExecutionBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    active_seconds: int = Field(ge=0, le=600)
    model_calls: int = Field(ge=0)
    mcp_calls: int = Field(ge=0)
    technical_retries: int = Field(ge=0, le=2)
    reflection_revision: int = Field(ge=0, le=1)


class ReflectionBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ReflectionStatus
    before_revision_id: UUID | None = None
    after_revision_id: UUID | None = None
    changes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_pair(self) -> ReflectionBlock:
        if self.status == ReflectionStatus.COMPLETED:
            if self.before_revision_id is None or self.after_revision_id is None:
                raise ValueError("completed reflection requires before and after revisions")
            if self.before_revision_id == self.after_revision_id:
                raise ValueError("reflection revisions must be distinct")
        return self


class ExecutionBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[ExecutionNode, ...] = Field(min_length=1)
    degradations: tuple[str, ...] = ()
    budget: ExecutionBudget
    reflection: ReflectionBlock
    result_ready: bool
    result_revision_id: UUID | None = None
    external_execution: Literal["not_executed"] = "not_executed"
    publication: Literal["not_executed"] = "not_executed"

    @model_validator(mode="after")
    def validate_result_ready(self) -> ExecutionBlock:
        if self.result_ready and self.result_revision_id is None:
            raise ValueError("ready results require a result revision")
        if self.result_revision_id is not None and not self.result_ready:
            raise ValueError("result revision requires result_ready")
        return self


class MemoryUpdateItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=20_000)
    source_evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
    scope: tuple[str, ...] = Field(min_length=1, max_length=100)
    trust_status: MemoryStatus
    write_status: Literal["not_executed"] = "not_executed"


class ProposalPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: PackageIdentity
    provenance: Provenance
    access: PackageAccess
    requirements: tuple[RequirementItem, ...]
    coverage: CoverageBlock
    evidence: tuple[EvidenceItem, ...]
    solution: SolutionBlock
    risks: tuple[RiskItem, ...]
    clarifications: tuple[ClarificationItem, ...]
    review: ReviewBlock
    approval: ApprovalBlock
    execution: ExecutionBlock
    memory_updates: tuple[MemoryUpdateItem, ...]

    @model_validator(mode="after")
    def validate_cross_references(self) -> ProposalPackage:
        evidence_ids = _unique_ids(self.evidence, "evidence")
        risk_ids = _unique_ids(self.risks, "risk")
        clarification_ids = _unique_ids(self.clarifications, "clarification")
        requirement_ids = _unique_ids(self.requirements, "requirement")

        declared_sources = set(self.access.direct_source_version_ids)
        for item in self.evidence:
            if item.source_version_id not in declared_sources:
                raise ValueError("evidence source is not declared in access scope")

        for requirement in self.requirements:
            _require_references(requirement.source_evidence_ids, evidence_ids, "requirement source")
            _require_references(requirement.evidence_ids, evidence_ids, "requirement evidence")
            _require_references(requirement.risk_ids, risk_ids, "requirement risk")
            _require_references(
                requirement.clarification_ids,
                clarification_ids,
                "requirement clarification",
            )

        for risk in self.risks:
            _require_references(risk.evidence_ids, evidence_ids, "risk evidence")
        for clarification in self.clarifications:
            _require_references(
                clarification.requirement_ids,
                requirement_ids,
                "clarification requirement",
            )
            _require_references(
                clarification.answer_evidence_ids,
                evidence_ids,
                "clarification evidence",
            )
            if clarification.confirmation is not None:
                _require_references(
                    clarification.confirmation.evidence_ids,
                    evidence_ids,
                    "confirmation evidence",
                )
        for memory in self.memory_updates:
            _require_references(memory.source_evidence_ids, evidence_ids, "memory evidence")

        expected = CoverageBlock.from_requirements(
            self.coverage.requirement_baseline_id, list(self.requirements)
        )
        if expected != self.coverage:
            raise ValueError("coverage block is not the deterministic requirement summary")
        return self

    def to_json(self) -> str:
        return canonical(self.model_dump(mode="json"))

    @classmethod
    def from_json(cls, value: str | bytes | Mapping[str, Any]) -> ProposalPackage:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        payload = json.loads(value) if isinstance(value, str) else dict(value)
        if _looks_like_legacy_draft(payload):
            raise ValueError("stage-one manual draft is not a complete Proposal Package")
        return cls.model_validate(payload)


def _unique_ids(items: tuple[Any, ...], label: str) -> set[Any]:
    values = [item.id for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} ids")
    return set(values)


def _require_references(references: tuple[Any, ...], available: set[Any], label: str) -> None:
    missing = set(references) - available
    if missing:
        raise ValueError(f"{label} references are unresolved")


def _looks_like_legacy_draft(payload: Mapping[str, Any]) -> bool:
    return (
        "draft" in payload
        or "incomplete_sections" in payload
        or (
            isinstance(payload.get("identity"), Mapping)
            and "analysis_attempt_id" in payload["identity"]
        )
    )


@dataclass(frozen=True)
class LegacyDraftAdaptation:
    schema_version: Literal["1.0"]
    draft: DraftContent
    missing_sections: tuple[str, ...]
    complete: bool = False


def adapt_legacy_draft(payload: Mapping[str, Any]) -> LegacyDraftAdaptation:
    if not _looks_like_legacy_draft(payload):
        raise ValueError("payload is not a stage-one manual draft")
    draft = DraftContent.model_validate(payload.get("draft", {}))
    missing = payload.get("incomplete_sections", [])
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise ValueError("legacy draft missing sections are malformed")
    return LegacyDraftAdaptation(
        schema_version="1.0",
        draft=draft,
        missing_sections=tuple(missing),
        complete=False,
    )


def proposal_package_json_schema() -> dict[str, Any]:
    return ProposalPackage.model_json_schema()


REQUIRED_BLOCKS = (
    "identity",
    "provenance",
    "access",
    "requirements",
    "coverage",
    "evidence",
    "solution",
    "risks",
    "clarifications",
    "review",
    "approval",
    "execution",
    "memory_updates",
)

__all__ = [
    "Applicability",
    "ApprovalBlock",
    "ClarificationItem",
    "ClarificationStatus",
    "Commitment",
    "CommitmentKind",
    "CoverageBlock",
    "CoverageCounts",
    "CoverageStatus",
    "DecisionReference",
    "EvidenceItem",
    "EvidenceLocation",
    "EvidenceSourceType",
    "ExecutionBlock",
    "ExecutionBudget",
    "ExecutionNode",
    "InputReference",
    "LegacyDraftAdaptation",
    "MemoryStatus",
    "MemoryUpdateItem",
    "NodeStatus",
    "PackageAccess",
    "PackageIdentity",
    "ProposalPackage",
    "Provenance",
    "REQUIRED_BLOCKS",
    "ReflectionBlock",
    "ReflectionStatus",
    "RequirementImportance",
    "RequirementItem",
    "RequirementScope",
    "RiskCategory",
    "RiskDisposition",
    "RiskItem",
    "RiskSeverity",
    "SolutionBlock",
    "adapt_legacy_draft",
    "proposal_package_json_schema",
]
