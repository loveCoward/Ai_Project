"""Offline contracts for the requirement node and the bounded Router plan."""

from uuid import uuid4

import pytest

from presales.application.requirement_analysis import (
    RequirementAnalysis,
    RequirementCapacityExceeded,
    RequirementRouter,
    plan_digest,
    plan_summary,
)
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


def origin(key="baseline-1") -> RequirementOrigin:
    return RequirementOrigin(
        source_version_id=uuid4(),
        evidence_id=uuid4(),
        locator={"page": "1"},
        baseline_requirement_key=key,
    )


def draft(text="支持单点登录", *keys: str) -> RequirementDraft:
    return RequirementDraft(
        text=text, origins=tuple(origin(key) for key in (keys or ("baseline-1",)))
    )


def test_full_plan_keeps_every_critical_expert_in_fixed_order():
    plan = RequirementRouter().plan("full_analysis", "required")
    assert [node.name for node in plan.nodes] == [
        "requirements",
        "evidence",
        "architecture",
        "risk",
        "summary",
        "reflection",
        "finalize",
    ]
    assert set(CRITICAL_EXPERTS) <= {node.name for node in plan.nodes}
    assert plan.digest if hasattr(plan, "digest") else plan_digest(plan)
    architecture = next(node for node in plan.nodes if node.name == "architecture")
    assert architecture.depends_on == ("requirements", "evidence")


def test_plan_summary_reports_nodes_and_digest():
    plan = full_analysis_plan("optional")
    summary = plan_summary(plan)
    assert summary["intent"] == "full_analysis"
    assert summary["background_requirement"] == "optional"
    assert summary["nodes"][0] == "requirements"
    assert len(summary["digest"]) == 64


def test_router_refuses_dropped_expert_and_duplicate_nodes():
    with pytest.raises(PlanNotAllowed) as dropped:
        RequirementRouter().plan(
            "full_analysis", "required", requested_nodes=("requirements", "evidence")
        )
    assert dropped.value.code == "plan_not_allowed"
    with pytest.raises(PlanNotAllowed) as duplicated:
        RequirementRouter().plan(
            "full_analysis",
            "required",
            requested_nodes=("requirements", "requirements", "evidence", "architecture", "risk"),
        )
    assert duplicated.value.code == "plan_not_allowed"


def test_router_requirements_only_returns_a_single_node():
    plan = RequirementRouter().plan("requirements_only", "optional")
    assert [node.name for node in plan.nodes] == ["requirements"]
    with pytest.raises(PlanNotAllowed):
        RequirementRouter().plan(
            "requirements_only", "optional", requested_nodes=("requirements", "risk")
        )


def test_plan_rejects_cycles_and_unknown_nodes():
    with pytest.raises(ValueError):
        AnalysisPlan(
            nodes=(
                PlanNode(name="requirements", depends_on=("risk",)),
                PlanNode(name="risk", depends_on=("requirements",)),
            ),
            background_requirement="required",
            intent="full_analysis",
        )
    with pytest.raises(ValueError):
        AnalysisPlan(
            nodes=(PlanNode(name="evidence", depends_on=("requirements",)),),
            background_requirement="required",
            intent="full_analysis",
        )


def test_requirements_get_server_ids_and_keep_origins():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    requirements = analysis.build(
        attempt,
        (draft("支持单点登录", "baseline-1"), draft("支持审计日志", "baseline-2")),
        baseline_keys=("baseline-1", "baseline-2"),
    )
    assert all(item.requirement_id.startswith("req-") for item in requirements)
    assert [
        origin.baseline_requirement_key for item in requirements for origin in item.origins
    ] == [
        "baseline-1",
        "baseline-2",
    ]
    assert {item.baseline_count for item in requirements} == {2}
    rebuilt = analysis.build(
        attempt,
        (draft("支持单点登录", "baseline-1"), draft("支持审计日志", "baseline-2")),
        baseline_keys=("baseline-1", "baseline-2"),
    )
    assert [item.requirement_id for item in rebuilt] == [
        item.requirement_id for item in requirements
    ]


def test_unknown_or_missing_origin_coverage_is_refused():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    with pytest.raises(RequirementProvenanceViolation) as unknown:
        analysis.build(
            attempt,
            (draft("支持单点登录", "baseline-9"),),
            baseline_keys=("baseline-1",),
        )
    assert unknown.value.code == "requirement_provenance_failed"
    with pytest.raises(RequirementProvenanceViolation):
        analysis.build(
            attempt,
            (draft("支持单点登录", "baseline-1"),),
            baseline_keys=("baseline-1", "baseline-2"),
        )


def test_duplicate_text_and_empty_baseline_are_refused():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    with pytest.raises(RequirementProvenanceViolation):
        analysis.build(
            attempt,
            (draft("支持单点登录", "baseline-1"), draft("支持单点登录", "baseline-1")),
            baseline_keys=("baseline-1",),
        )
    with pytest.raises(RequirementProvenanceViolation):
        analysis.build(attempt, (draft("支持单点登录"),), baseline_keys=())


def test_merge_keeps_origins_and_does_not_reduce_the_baseline():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    requirements = analysis.build(
        attempt,
        (draft("支持单点登录", "baseline-1"), draft("支持审计日志", "baseline-2")),
        baseline_keys=("baseline-1", "baseline-2"),
    )
    merged = analysis.merge(
        attempt, requirements, indices=(0, 1), text="支持单点登录并输出审计日志"
    )
    assert len(merged) == 1
    assert merged[0].baseline_count == 2
    assert {origin.baseline_requirement_key for origin in merged[0].origins} == {
        "baseline-1",
        "baseline-2",
    }
    assert set(merged[0].derived_from) == {item.requirement_id for item in requirements}


def test_merge_of_a_single_item_is_refused():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    requirements = analysis.build(
        attempt, (draft("支持单点登录", "baseline-1"),), baseline_keys=("baseline-1",)
    )
    with pytest.raises(RequirementProvenanceViolation):
        analysis.merge(attempt, requirements, indices=(0,), text="合并后")


def test_split_keeps_every_original_origin():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    requirements = analysis.build(
        attempt, (draft("支持单点登录与审计日志", "baseline-1"),), baseline_keys=("baseline-1",)
    )
    pieces = analysis.split(
        attempt,
        requirements,
        index=0,
        texts=("支持单点登录", "支持审计日志"),
    )
    assert len(pieces) == 2
    assert all(piece.baseline_count == 1 for piece in pieces)
    assert all(
        origin.baseline_requirement_key == "baseline-1"
        for piece in pieces
        for origin in piece.origins
    )
    assert {piece.derived_from for piece in pieces} == {(requirements[0].requirement_id,)}


def test_split_with_one_piece_is_refused():
    attempt = uuid4()
    analysis = RequirementAnalysis()
    requirements = analysis.build(
        attempt, (draft("支持单点登录", "baseline-1"),), baseline_keys=("baseline-1",)
    )
    with pytest.raises(RequirementProvenanceViolation):
        analysis.split(attempt, requirements, index=0, texts=("只有一条",))


def test_requirement_capacity_refuses_instead_of_truncating():
    attempt = uuid4()
    analysis = RequirementAnalysis(max_characters=100)
    with pytest.raises(RequirementCapacityExceeded) as over:
        analysis.build(
            attempt,
            (RequirementDraft(text="x" * 101, origins=(origin(),)),),
            baseline_keys=("baseline-1",),
        )
    assert over.value.code == "capacity_exceeded"
    accepted = analysis.build(
        attempt,
        (RequirementDraft(text="x" * 100, origins=(origin(),)),),
        baseline_keys=("baseline-1",),
    )
    assert accepted[0].text == "x" * 100


def test_requirement_id_cannot_be_invented_by_the_caller():
    with pytest.raises(ValueError):
        Requirement(
            requirement_id="baseline-1",
            text="支持单点登录",
            origins=(origin(),),
            baseline_count=1,
        )


def test_capacity_bounds_must_be_positive():
    with pytest.raises(ValueError):
        RequirementAnalysis(max_characters=0)
