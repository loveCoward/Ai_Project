"""Requirement identity, provenance and the bounded Router analysis plan."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from presales.domain.contracts import DomainError

CRITICAL_EXPERTS: tuple[str, ...] = (
    "requirements",
    "evidence",
    "architecture",
    "risk",
)

ALLOWED_NODES: tuple[str, ...] = CRITICAL_EXPERTS + ("summary", "reflection", "finalize")


class PlanNotAllowed(DomainError):
    code = "plan_not_allowed"


class RequirementProvenanceViolation(DomainError):
    code = "requirement_provenance_failed"


class RequirementOrigin(BaseModel):
    """Original place a requirement came from; never rewritten by split or merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_version_id: UUID
    evidence_id: UUID
    locator: dict[str, str] = Field(default_factory=dict)
    baseline_requirement_key: str = Field(min_length=1, max_length=200)


class RequirementDraft(BaseModel):
    """Model-proposed requirement text; identity is assigned by the server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=20_000)
    origins: tuple[RequirementOrigin, ...] = Field(min_length=1, max_length=100)
    derived_from: tuple[str, ...] = ()


class Requirement(BaseModel):
    """Server-owned requirement with a stable identity and preserved origins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)
    origins: tuple[RequirementOrigin, ...] = Field(min_length=1, max_length=100)
    derived_from: tuple[str, ...] = ()
    baseline_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "Requirement":
        if not self.requirement_id.startswith("req-"):
            raise ValueError("server-issued requirement ids use the req- prefix")
        return self


class PlanNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal[
        "requirements", "evidence", "architecture", "risk", "summary", "reflection", "finalize"
    ]
    depends_on: tuple[str, ...] = ()


class AnalysisPlan(BaseModel):
    """Router output: a fixed expert order with declared dependencies and no cycles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[PlanNode, ...]
    background_requirement: Literal["required", "optional", "unconfirmed"]
    intent: Literal["full_analysis", "requirements_only"]

    @model_validator(mode="after")
    def validate_plan(self) -> "AnalysisPlan":
        names: list[str] = [node.name for node in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError("duplicate plan node")
        if set(names) - set(ALLOWED_NODES):
            raise ValueError("plan contains an unlisted node")
        if self.intent == "full_analysis" and not set(CRITICAL_EXPERTS) <= set(names):
            raise ValueError("full analysis must keep every critical expert")
        if self.intent == "requirements_only" and names != ["requirements"]:
            raise ValueError("requirements-only plans contain exactly one node")
        self._reject_cycle(names)
        return self

    def _reject_cycle(self, names: list[str]) -> None:
        graph: dict[str, set[str]] = {node.name: set(node.depends_on) for node in self.nodes}
        unknown = {parent for parents in graph.values() for parent in parents} - set(names)
        if unknown:
            raise ValueError("plan depends on a missing node")
        visited: dict[str, int] = {}
        for start in names:
            stack = [(start, iter(graph[start]))]
            visited[start] = 1
            while stack:
                node, children = stack[-1]
                child = next(children, None)
                if child is None:
                    visited[node] = 2
                    stack.pop()
                    continue
                if visited.get(child) == 1:
                    raise ValueError("plan contains a cycle")
                if visited.get(child) != 2:
                    visited[child] = 1
                    stack.append((child, iter(graph[child])))


def full_analysis_plan(
    background_requirement: Literal["required", "optional", "unconfirmed"],
) -> AnalysisPlan:
    """Deterministic whitelist order accepted by the design document."""

    return AnalysisPlan(
        nodes=(
            PlanNode(name="requirements"),
            PlanNode(name="evidence", depends_on=("requirements",)),
            PlanNode(name="architecture", depends_on=("requirements", "evidence")),
            PlanNode(name="risk", depends_on=("requirements", "evidence", "architecture")),
            PlanNode(
                name="summary", depends_on=("requirements", "evidence", "architecture", "risk")
            ),
            PlanNode(name="reflection", depends_on=("summary",)),
            PlanNode(name="finalize", depends_on=("reflection",)),
        ),
        background_requirement=background_requirement,
        intent="full_analysis",
    )


__all__ = [
    "ALLOWED_NODES",
    "AnalysisPlan",
    "CRITICAL_EXPERTS",
    "PlanNode",
    "PlanNotAllowed",
    "Requirement",
    "RequirementDraft",
    "RequirementOrigin",
    "RequirementProvenanceViolation",
    "full_analysis_plan",
]
