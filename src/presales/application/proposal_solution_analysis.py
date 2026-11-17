"""Architecture node: turns requirements and evidence into a bounded solution draft."""

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from presales.application.evidence_analysis import EvidenceFinding
from presales.domain.contracts import DomainError
from presales.domain.requirement_plan import Requirement

StatementKind = Literal["fact", "advice", "pending_confirmation"]
CommitmentKind = Literal["quote", "schedule", "contract"]


class SolutionEvidenceUnavailable(DomainError):
    code = "solution_evidence_unavailable"


class SolutionRequirementsEmpty(DomainError):
    code = "solution_requirements_empty"


class SolutionStructureMissing(DomainError):
    code = "solution_structure_missing"


class SolutionCapacityExceeded(DomainError):
    code = "solution_capacity_exceeded"


class SolutionContractViolation(DomainError):
    code = "solution_contract_failed"


class SolutionStatement(BaseModel):
    """One classified claim; every claim must stay attributable to evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    kind: StatementKind
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)
    requirement_ids: tuple[str, ...] = ()


class CommitmentSuggestion(BaseModel):
    """Suggestions can never bind or be treated as confirmed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CommitmentKind
    text: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = ()
    binding: Literal[False] = False
    confirmation_required: Literal[True] = True


class UnsupportedRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(min_length=1)
    limitation: str = Field(min_length=1)
    text: str = ""


class SolutionDraft(BaseModel):
    """Model-proposed structure; completeness is enforced by the node, not here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommended: str | None = None
    deployment_integration: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    delivery_notes: tuple[str, ...] = ()
    statements: tuple[SolutionStatement, ...] = ()
    commitments: tuple[CommitmentSuggestion, ...] = ()
    unsupported: tuple[UnsupportedRef, ...] = ()


class SolutionAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recommended: str
    deployment_integration: tuple[str, ...]
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    delivery_notes: tuple[str, ...]
    statements: tuple[SolutionStatement, ...]
    commitments: tuple[CommitmentSuggestion, ...]
    unsupported: tuple[UnsupportedRef, ...]
    executed_checks: tuple[str, ...]


class ProposalSolutionAnalysis:
    """Validates an architecture draft; it cannot retrieve, approve or bind."""

    def __init__(self, *, max_characters: int = 60_000) -> None:
        if max_characters < 1:
            raise ValueError("Solution capacity must be positive")
        self.max_characters = max_characters

    async def analyse(
        self,
        requirements: Sequence[Requirement],
        evidence: EvidenceFinding,
        draft: SolutionDraft,
    ) -> SolutionAnalysis:
        ordered = tuple(requirements)
        if not ordered:
            raise SolutionRequirementsEmpty()
        if sum(len(item.text) for item in ordered) > self.max_characters:
            raise SolutionCapacityExceeded()
        if evidence.status in {"unavailable", "conflict", "not_found_or_not_allowed"}:
            raise SolutionEvidenceUnavailable()

        recommended = self._required_text(draft.recommended)
        deployment_integration = self._required_parts(draft.deployment_integration)
        assumptions = self._required_parts(draft.assumptions)
        limitations = self._required_parts(draft.limitations)

        known = {item.requirement_id: item for item in ordered}
        unsupported_ids = {item.requirement_id for item in draft.unsupported}
        if unsupported_ids - set(known):
            raise SolutionContractViolation()
        if any(
            unsupported_ids & set(commitment.requirement_ids) for commitment in draft.commitments
        ):
            raise SolutionContractViolation()
        if any(unsupported_ids & set(statement.requirement_ids) for statement in draft.statements):
            raise SolutionContractViolation()

        cited = {hit.evidence_id for hit in evidence.hits}
        for statement in draft.statements:
            if set(statement.evidence_ids) - cited:
                raise SolutionContractViolation()

        checks = [
            "unsupported_preserved",
            "commitments_non_binding",
            "statement_classification",
        ]
        if evidence.status == "degraded":
            checks.append("evidence_degraded")
        elif evidence.status == "empty":
            checks.append("evidence_empty")

        return SolutionAnalysis(
            recommended=recommended,
            deployment_integration=deployment_integration,
            assumptions=assumptions,
            limitations=limitations,
            delivery_notes=draft.delivery_notes,
            statements=draft.statements,
            commitments=draft.commitments,
            unsupported=tuple(
                UnsupportedRef(
                    requirement_id=item.requirement_id,
                    limitation=item.limitation,
                    text=known[item.requirement_id].text,
                )
                for item in draft.unsupported
            ),
            executed_checks=tuple(checks),
        )

    @staticmethod
    def _required_text(value: str | None) -> str:
        if value is None or not value.strip():
            raise SolutionStructureMissing()
        return value

    @staticmethod
    def _required_parts(value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not part.strip() for part in value):
            raise SolutionStructureMissing()
        return value


__all__ = [
    "CommitmentKind",
    "CommitmentSuggestion",
    "ProposalSolutionAnalysis",
    "SolutionAnalysis",
    "SolutionCapacityExceeded",
    "SolutionContractViolation",
    "SolutionDraft",
    "SolutionEvidenceUnavailable",
    "SolutionRequirementsEmpty",
    "SolutionStatement",
    "SolutionStructureMissing",
    "StatementKind",
    "UnsupportedRef",
]
