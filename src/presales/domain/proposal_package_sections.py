"""Source, evidence, requirement and coverage sections of Proposal Package Schema 1.0."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from presales.domain.contracts import SourceDeclaration
from presales.domain.proposal_analysis_attempt import ModelMode


class RequirementImportance(StrEnum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class RequirementScope(StrEnum):
    CONFIRMED_IN_SCOPE = "confirmed_in_scope"
    OUTSIDE_CONFIRMED_SCOPE = "outside_confirmed_scope"
    UNCONFIRMED = "unconfirmed"


class CoverageStatus(StrEnum):
    COVERED = "covered"
    PARTIALLY_COVERED = "partially_covered"
    UNCOVERED = "uncovered"
    CLARIFICATION_NEEDED = "clarification_needed"
    OUT_OF_SCOPE = "out_of_scope"


class EvidenceSourceType(StrEnum):
    CUSTOMER_MATERIAL = "customer_material"
    PRODUCT = "product"
    TECHNICAL = "technical"
    SECURITY = "security"
    DEPLOYMENT = "deployment"
    DELIVERY = "delivery"
    CASE_STUDY = "case_study"


class RiskCategory(StrEnum):
    SECURITY_COMPLIANCE = "security_compliance"
    DEPLOYMENT = "deployment"
    INTEGRATION = "integration"
    DATA = "data"
    DELIVERY = "delivery"
    PRODUCT_CAPABILITY = "product_capability"


class RiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SEVERE = "severe"


class RiskDisposition(StrEnum):
    PENDING = "pending"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"


class ClarificationStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class NodeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED = "degraded"
    SKIPPED = "skipped"


class ReflectionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class MemoryStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    CONFLICTING = "conflicting"


class CommitmentKind(StrEnum):
    QUOTE = "quote"
    SCHEDULE = "schedule"
    CONTRACT = "contract"


_SENSITIVE_MARKERS = ("api_key=", "apikey=", "token=", "password=", "secret=")


def _reject_embedded_secret(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ValueError("provenance cannot contain credentials")
    return value


class PackageIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    customer_id: UUID
    opportunity_id: UUID
    task_id: UUID
    attempt_id: UUID
    proposal_version_id: UUID
    content_revision: int = Field(ge=1)


class InputReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_version_id: UUID
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_kind: Literal["customer_material", "knowledge", "proposal"]


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: tuple[InputReference, ...] = Field(min_length=1, max_length=1000)
    model_mode: ModelMode
    model_identity: str = Field(min_length=1, max_length=200)
    endpoint_identity: str = Field(min_length=1, max_length=200)
    template_version: str = Field(min_length=1, max_length=100)
    config_identity: str = Field(min_length=1, max_length=200)
    generated_at: datetime

    _no_secrets = field_validator(
        "model_identity", "endpoint_identity", "template_version", "config_identity"
    )(_reject_embedded_secret)


class PackageAccess(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_subject_id: UUID
    direct_source_version_ids: tuple[UUID, ...] = Field(min_length=1, max_length=2000)
    derived_artifact_scope: tuple[str, ...] = Field(min_length=1)
    restricted_read_note: str = Field(min_length=1, max_length=1000)


class EvidenceLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["markdown_paragraph", "pdf_page", "json_path", "table_cell"]
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)
    table: str | None = Field(default=None, max_length=200)
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    path: str | None = Field(default=None, max_length=500)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_locator_shape(self) -> EvidenceLocation:
        if self.start_offset is not None and self.end_offset is not None:
            if self.end_offset < self.start_offset:
                raise ValueError("evidence offset range is reversed")
        elif (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("evidence offsets must be provided together")

        if self.kind == "markdown_paragraph":
            valid = self.paragraph is not None and self.page is None and self.path is None
        elif self.kind == "pdf_page":
            valid = self.page is not None and self.paragraph is None and self.path is None
        elif self.kind == "json_path":
            valid = self.path is not None and self.page is None and self.paragraph is None
        else:
            valid = (
                self.table is not None
                and self.row is not None
                and self.column is not None
                and self.page is None
                and self.paragraph is None
                and self.path is None
            )
        if not valid:
            raise ValueError("evidence locator does not match its kind")
        return self


class Applicability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_scope: tuple[str, ...] = Field(min_length=1, max_length=100)
    deployment_scope: tuple[str, ...] = Field(min_length=1, max_length=100)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    source_version_id: UUID
    source_type: EvidenceSourceType
    declaration: SourceDeclaration
    location: EvidenceLocation
    fragment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str = Field(default="", max_length=100_000)
    applicability: Applicability
    authorization_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def require_safe_declaration(self) -> EvidenceItem:
        if self.declaration in {
            SourceDeclaration.SENSITIVE,
            SourceDeclaration.UNKNOWN,
        }:
            raise ValueError("evidence must have a readable source declaration")
        return self


class RequirementItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)
    source_evidence_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
    importance: RequirementImportance
    scope_status: RequirementScope
    coverage_status: CoverageStatus
    evidence_ids: tuple[UUID, ...] = ()
    risk_ids: tuple[str, ...] = ()
    clarification_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_coverage_support(self) -> RequirementItem:
        if self.scope_status == RequirementScope.OUTSIDE_CONFIRMED_SCOPE and (
            self.coverage_status in {CoverageStatus.COVERED, CoverageStatus.PARTIALLY_COVERED}
        ):
            raise ValueError("outside-scope requirements cannot be counted as covered")
        if self.coverage_status == CoverageStatus.COVERED and not self.evidence_ids:
            raise ValueError("covered requirements require evidence")
        if self.coverage_status == CoverageStatus.PARTIALLY_COVERED and not (
            self.evidence_ids or self.clarification_ids
        ):
            raise ValueError("partially covered requirements require support or clarification")
        if (
            self.coverage_status == CoverageStatus.CLARIFICATION_NEEDED
            and not self.clarification_ids
        ):
            raise ValueError("clarification-needed requirements require a clarification")
        return self


class CoverageCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    covered: int = Field(ge=0)
    partially_covered: int = Field(ge=0)
    uncovered: int = Field(ge=0)
    clarification_needed: int = Field(ge=0)
    out_of_scope: int = Field(ge=0)

    def total(self) -> int:
        return (
            self.covered
            + self.partially_covered
            + self.uncovered
            + self.clarification_needed
            + self.out_of_scope
        )


class CoverageBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_baseline_id: UUID
    total_requirements: int = Field(ge=0)
    fully_covered: int = Field(ge=0)
    counts: CoverageCounts
    business_ratio: float | None = Field(default=None, ge=0, le=1)
    applicable: bool

    @model_validator(mode="after")
    def validate_counts(self) -> CoverageBlock:
        if self.counts.total() != self.total_requirements:
            raise ValueError("coverage counts do not match the denominator")
        if self.fully_covered != self.counts.covered:
            raise ValueError("fully covered numerator does not match covered count")
        expected = self.fully_covered / self.total_requirements if self.total_requirements else None
        if self.business_ratio != expected:
            raise ValueError("business ratio must be deterministic")
        if self.applicable != bool(self.total_requirements):
            raise ValueError("empty requirement scope is not applicable")
        return self

    @classmethod
    def from_requirements(
        cls, requirement_baseline_id: UUID, requirements: list[RequirementItem]
    ) -> CoverageBlock:
        counts = CoverageCounts(
            covered=sum(item.coverage_status == CoverageStatus.COVERED for item in requirements),
            partially_covered=sum(
                item.coverage_status == CoverageStatus.PARTIALLY_COVERED for item in requirements
            ),
            uncovered=sum(
                item.coverage_status == CoverageStatus.UNCOVERED for item in requirements
            ),
            clarification_needed=sum(
                item.coverage_status == CoverageStatus.CLARIFICATION_NEEDED for item in requirements
            ),
            out_of_scope=sum(
                item.coverage_status == CoverageStatus.OUT_OF_SCOPE for item in requirements
            ),
        )
        total = len(requirements)
        return cls(
            requirement_baseline_id=requirement_baseline_id,
            total_requirements=total,
            fully_covered=counts.covered,
            counts=counts,
            business_ratio=counts.covered / total if total else None,
            applicable=bool(total),
        )
