"""Builds stable, provenance-preserving requirements and the bounded Router plan."""

from collections.abc import Iterable
from typing import Any, Literal
from uuid import UUID

from presales.domain.contracts import InvalidInput, digest
from presales.domain.requirement_plan import (
    CRITICAL_EXPERTS,
    AnalysisPlan,
    PlanNode,
    PlanNotAllowed,
    Requirement,
    RequirementDraft,
    RequirementOrigin,
    RequirementProvenanceViolation,
    full_analysis_plan,
)


class RequirementCapacityExceeded(InvalidInput):
    code = "capacity_exceeded"


class RequirementRouter:
    """Only chooses among approved node sets; it cannot add tools, drop experts or loop."""

    def plan(
        self,
        intent: Literal["full_analysis", "requirements_only"],
        background_requirement: Literal["required", "optional", "unconfirmed"],
        *,
        requested_nodes: Iterable[str] | None = None,
    ) -> AnalysisPlan:
        if intent == "requirements_only":
            if requested_nodes is not None and set(requested_nodes) != {"requirements"}:
                raise PlanNotAllowed()
            return AnalysisPlan(
                nodes=(PlanNode(name="requirements"),),
                background_requirement=background_requirement,
                intent="requirements_only",
            )
        if requested_nodes is not None:
            requested = tuple(requested_nodes)
            if len(requested) != len(set(requested)) or not set(CRITICAL_EXPERTS) <= set(requested):
                raise PlanNotAllowed()
        return full_analysis_plan(background_requirement)


class RequirementAnalysis:
    """Server-side requirement identity; split and merge keep every original origin."""

    def __init__(self, *, max_characters: int = 60_000) -> None:
        if max_characters < 1:
            raise ValueError("Requirement capacity must be positive")
        self.max_characters = max_characters

    def build(
        self,
        attempt_id: UUID,
        drafts: Iterable[RequirementDraft],
        *,
        baseline_keys: Iterable[str],
    ) -> tuple[Requirement, ...]:
        collected = tuple(drafts)
        self._require_capacity(collected)
        keys = tuple(baseline_keys)
        if not keys or len(set(keys)) != len(keys):
            raise RequirementProvenanceViolation()
        requirements: list[Requirement] = []
        seen_text: set[str] = set()
        for ordinal, draft in enumerate(collected, start=1):
            if draft.text in seen_text:
                raise RequirementProvenanceViolation()
            seen_text.add(draft.text)
            for origin in draft.origins:
                if origin.baseline_requirement_key not in keys:
                    raise RequirementProvenanceViolation()
            requirements.append(
                Requirement(
                    requirement_id=self._identifier(attempt_id, ordinal, draft.text),
                    text=draft.text,
                    origins=draft.origins,
                    derived_from=draft.derived_from,
                    baseline_count=len(keys),
                )
            )
        covered = {
            origin.baseline_requirement_key for item in requirements for origin in item.origins
        }
        if covered != set(keys):
            raise RequirementProvenanceViolation()
        return tuple(requirements)

    def merge(
        self,
        attempt_id: UUID,
        requirements: tuple[Requirement, ...],
        *,
        indices: tuple[int, ...],
        text: str,
    ) -> tuple[Requirement, ...]:
        if len(indices) < 2 or len(set(indices)) != len(indices):
            raise RequirementProvenanceViolation()
        try:
            selected = [requirements[index] for index in indices]
        except IndexError:
            raise RequirementProvenanceViolation() from None
        baseline_counts = {item.baseline_count for item in selected}
        if len(baseline_counts) != 1:
            raise RequirementProvenanceViolation()
        kept = [item for index, item in enumerate(requirements) if index not in set(indices)]
        reopened = {key for item in kept for key in _keys(item)}
        merged_origins: dict[tuple, RequirementOrigin] = {}
        for item in selected:
            for origin in item.origins:
                if origin.baseline_requirement_key in reopened:
                    continue
                key = (
                    origin.source_version_id,
                    origin.evidence_id,
                    origin.baseline_requirement_key,
                )
                merged_origins.setdefault(key, origin)
        origins = tuple(merged_origins.values())
        merged = Requirement(
            requirement_id=self._identifier(attempt_id, len(requirements) + 1, text),
            text=text,
            origins=origins or tuple(origin for item in selected for origin in item.origins),
            derived_from=tuple(item.requirement_id for item in selected),
            baseline_count=baseline_counts.pop(),
        )
        return tuple(kept) + (merged,)

    def split(
        self,
        attempt_id: UUID,
        requirements: tuple[Requirement, ...],
        *,
        index: int,
        texts: tuple[str, ...],
    ) -> tuple[Requirement, ...]:
        if len(texts) < 2:
            raise RequirementProvenanceViolation()
        try:
            original = requirements[index]
        except IndexError:
            raise RequirementProvenanceViolation() from None
        pieces = tuple(
            Requirement(
                requirement_id=self._identifier(attempt_id, len(requirements) + position, text),
                text=text,
                origins=original.origins,
                derived_from=(original.requirement_id,),
                baseline_count=original.baseline_count,
            )
            for position, text in enumerate(texts, start=1)
        )
        return requirements[:index] + pieces + requirements[index + 1 :]

    def require_capacity(self, text: str) -> None:
        if len(text) > self.max_characters:
            raise RequirementCapacityExceeded()

    def _require_capacity(self, drafts: tuple[RequirementDraft, ...]) -> None:
        total = sum(len(draft.text) for draft in drafts)
        if total > self.max_characters:
            raise RequirementCapacityExceeded()

    @staticmethod
    def _identifier(attempt_id: UUID, ordinal: int, text: str) -> str:
        return f"req-{digest({'attempt': str(attempt_id), 'ordinal': ordinal, 'text': text})[:24]}"


def _keys(requirement: Requirement) -> set[str]:
    return {origin.baseline_requirement_key for origin in requirement.origins}


def plan_digest(plan: AnalysisPlan) -> str:
    return digest(plan.model_dump(mode="json"))


def plan_summary(plan: AnalysisPlan) -> dict[str, Any]:
    return {
        "intent": plan.intent,
        "background_requirement": plan.background_requirement,
        "nodes": [node.name for node in plan.nodes],
        "digest": plan_digest(plan),
    }


__all__ = [
    "RequirementAnalysis",
    "RequirementCapacityExceeded",
    "RequirementRouter",
    "plan_digest",
    "plan_summary",
]
