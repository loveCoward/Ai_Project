"""Offline contracts for the 2B-2/T18 architecture node.

The node consumes only the frozen upstream contracts: ``Requirement`` from
``presales.domain.requirement_plan`` and ``EvidenceFinding`` from
``presales.application.evidence_analysis``. No port, database, model, browser or
MCP process participates.

``task.toml`` [interface] fixes semantics but not names, so this file freezes the
public surface the implementer must provide:

- ``ProposalSolutionAnalysis(max_characters=60000)`` with
  ``async analyse(requirements, evidence, draft) -> SolutionAnalysis``
- inputs ``SolutionDraft``, ``SolutionStatement``, ``CommitmentSuggestion``,
  ``UnsupportedRef``; output ``SolutionAnalysis``
- stable failures ``SolutionEvidenceUnavailable`` (``solution_evidence_unavailable``),
  ``SolutionRequirementsEmpty`` (``solution_requirements_empty``),
  ``SolutionStructureMissing`` (``solution_structure_missing``),
  ``SolutionCapacityExceeded`` (``solution_capacity_exceeded``) and
  ``SolutionContractViolation`` (``solution_contract_failed``)

Rules frozen by the AC matrix:

- AC09-a: every statement carries one of ``fact`` / ``advice`` /
  ``pending_confirmation`` and at least one ``evidence_id``; every cited id must
  belong to the supplied ``EvidenceFinding``, otherwise the node violates the
  contract.
- AC07-b: commitment suggestions (``quote`` / ``schedule`` / ``contract``) are
  always non-binding and confirmation-required; the result exposes no approval or
  approved field.
- AC19-a: structural completeness is enforced by the node, not by draft
  construction. ``SolutionDraft`` therefore declares ``recommended``,
  ``deployment_integration``, ``assumptions`` and ``limitations`` without
  length/min-length constraints (``None`` or ``()`` must be accepted), so an
  incomplete draft reaches ``analyse`` and is rejected there.
- AC19-b: capacity counts the requirement text characters and fails before any
  output; nothing is truncated and no retry happens.

``SolutionAnalysis.executed_checks`` must report at least
``unsupported_preserved``, ``commitments_non_binding``, ``statement_classification``
and, for a degraded finding, ``evidence_degraded``.
"""

import importlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from presales.application.evidence_analysis import EvidenceFinding, EvidenceHit
from presales.domain.requirement_plan import Requirement, RequirementOrigin
from presales.knowledge.models import Applicability

SCOPE = Applicability(products=("synthetic-product",), deployments=("private",))
QUERY_DIGEST = "0" * 64


def solution_node():
    """Delayed import keeps the file collectable while the module is missing."""
    return importlib.import_module("presales.application.proposal_solution_analysis")


def requirement(requirement_id="req-1", text="支持私有部署"):
    return Requirement(
        requirement_id=requirement_id,
        text=text,
        origins=(
            RequirementOrigin(
                source_version_id=uuid4(),
                evidence_id=uuid4(),
                locator={"page": "1"},
                baseline_requirement_key="baseline-1",
            ),
        ),
        baseline_count=1,
    )


def finding(*texts, status="content"):
    hits = tuple(
        EvidenceHit(
            evidence_id=uuid4(),
            source_version_id=uuid4(),
            declaration="synthetic",
            applicability=SCOPE,
            locator={"kind": "pdf_page", "page": 3},
            text=text,
            score=0.9,
            origin="knowledge",
        )
        for text in (texts or ("私有部署支持审计日志",))
    )
    kept = hits if status in {"content", "degraded"} else ()
    return EvidenceFinding(
        query_digest=QUERY_DIGEST,
        status=status,
        hits=kept,
        checked=("published_knowledge_scope", "published_catalog_scope"),
    )


def draft_with(solution, **overrides):
    payload = {
        "recommended": "私有化部署双节点方案，审计与身份集成按客户环境确认",
        "deployment_integration": ("与客户身份提供方做OIDC集成",),
        "assumptions": ("客户提供可用测试环境",),
        "limitations": ("不支持将审计日志实时外送客户SIEM",),
        "delivery_notes": ("上线前完成一次容灾演练",),
    }
    payload.update(overrides)
    return solution.SolutionDraft(**payload)


def fact(solution, text, evidence_id):
    return solution.SolutionStatement(text=text, kind="fact", evidence_ids=(evidence_id,))


@pytest.mark.asyncio
async def test_unsupported_requirement_is_not_replaced_with_a_commitment():
    solution = solution_node()
    unsupported = requirement("req-1", "支持将审计日志实时推送到客户自有SIEM")
    supported = requirement("req-2", "支持私有部署")
    evidence = finding()
    evidence_id = evidence.hits[0].evidence_id

    draft = draft_with(
        solution,
        statements=(fact(solution, "产品支持私有部署", evidence_id),),
        unsupported=(
            solution.UnsupportedRef(
                requirement_id="req-1",
                limitation="当前产品不支持将审计日志实时外送客户SIEM，只能导出离线文件",
            ),
        ),
    )
    result = await solution.ProposalSolutionAnalysis().analyse(
        (unsupported, supported), evidence, draft
    )

    assert [item.requirement_id for item in result.unsupported] == ["req-1"]
    assert result.unsupported[0].text == unsupported.text
    assert "不支持" in result.unsupported[0].limitation
    assert result.commitments == ()
    assert all("req-1" not in item.requirement_ids for item in result.statements)
    assert "req-1" not in " ".join(result.deployment_integration)
    assert "unsupported_preserved" in result.executed_checks

    replacing = draft_with(
        solution,
        statements=draft.statements,
        unsupported=draft.unsupported,
        commitments=(
            solution.CommitmentSuggestion(
                kind="quote",
                text="SIEM 实时外送由我方定制开发补齐",
                requirement_ids=("req-1",),
            ),
        ),
    )
    with pytest.raises(solution.SolutionContractViolation) as error:
        await solution.ProposalSolutionAnalysis().analyse(
            (unsupported, supported), evidence, replacing
        )
    assert error.value.code == "solution_contract_failed"


@pytest.mark.asyncio
async def test_quote_and_schedule_suggestions_stay_non_binding():
    solution = solution_node()
    quote = solution.CommitmentSuggestion(kind="quote", text="license 费用按节点数另行报价")
    schedule = solution.CommitmentSuggestion(kind="schedule", text="预计四周内完成部署")
    contract = solution.CommitmentSuggestion(kind="contract", text="需求条款以正式合同为准")
    assert quote.binding is False and quote.confirmation_required is True
    assert schedule.binding is False and schedule.confirmation_required is True

    evidence = finding()
    evidence_id = evidence.hits[0].evidence_id
    draft = draft_with(
        solution,
        statements=(fact(solution, "产品支持私有部署", evidence_id),),
        commitments=(quote, schedule, contract),
    )
    result = await solution.ProposalSolutionAnalysis().analyse((requirement(),), evidence, draft)

    assert [item.kind for item in result.commitments] == ["quote", "schedule", "contract"]
    for item in result.commitments:
        assert item.binding is False
        assert item.confirmation_required is True
    assert not any(
        name.startswith("approval") or name == "approved"
        for name in solution.SolutionAnalysis.model_fields
    )
    assert "commitments_non_binding" in result.executed_checks

    with pytest.raises(ValidationError):
        solution.CommitmentSuggestion(kind="quote", text="绑定的报价承诺", binding=True)
    with pytest.raises(ValidationError):
        solution.CommitmentSuggestion(
            kind="schedule", text="已确认的工期", confirmation_required=False
        )


@pytest.mark.asyncio
async def test_every_statement_carries_a_kind_and_a_source():
    solution = solution_node()
    evidence = finding("私有部署支持审计日志，支持OIDC集成")
    evidence_id = evidence.hits[0].evidence_id
    draft = draft_with(
        solution,
        statements=(
            fact(solution, "产品支持私有部署下的审计日志", evidence_id),
            solution.SolutionStatement(
                text="建议先接入身份提供方再迁移审计数据",
                kind="advice",
                evidence_ids=(evidence_id,),
                requirement_ids=("req-1",),
            ),
            solution.SolutionStatement(
                text="客户SIEM的对接窗口尚待客户确认",
                kind="pending_confirmation",
                evidence_ids=(evidence_id,),
            ),
        ),
    )
    result = await solution.ProposalSolutionAnalysis().analyse((requirement(),), evidence, draft)

    assert {item.kind for item in result.statements} == {
        "fact",
        "advice",
        "pending_confirmation",
    }
    assert all(item.evidence_ids for item in result.statements)
    assert "statement_classification" in result.executed_checks

    with pytest.raises(ValidationError) as unsourced:
        solution.SolutionStatement(text="产品支持SIEM实时外送", kind="fact")
    assert "evidence_ids" in str(unsourced.value)

    invented = draft_with(
        solution,
        statements=(fact(solution, "产品支持SIEM实时外送", uuid4()),),
    )
    with pytest.raises(solution.SolutionContractViolation) as error:
        await solution.ProposalSolutionAnalysis().analyse((requirement(),), evidence, invented)
    assert error.value.code == "solution_contract_failed"


@pytest.mark.asyncio
async def test_unusable_evidence_blocks_the_architecture_node():
    solution = solution_node()
    evidence_id = finding().hits[0].evidence_id
    draft = draft_with(
        solution,
        statements=(fact(solution, "产品支持私有部署", evidence_id),),
    )

    for status in ("unavailable", "conflict", "not_found_or_not_allowed"):
        with pytest.raises(solution.SolutionEvidenceUnavailable) as error:
            await solution.ProposalSolutionAnalysis().analyse(
                (requirement(),), finding(status=status), draft
            )
        assert error.value.code == "solution_evidence_unavailable"

    with pytest.raises(solution.SolutionRequirementsEmpty) as empty:
        await solution.ProposalSolutionAnalysis().analyse((), finding(), draft)
    assert empty.value.code == "solution_requirements_empty"

    degraded_evidence = finding("私有部署支持审计日志", status="degraded")
    degraded_draft = draft_with(
        solution,
        statements=(
            fact(
                solution,
                "产品支持私有部署",
                degraded_evidence.hits[0].evidence_id,
            ),
        ),
    )
    degraded = await solution.ProposalSolutionAnalysis().analyse(
        (requirement(),), degraded_evidence, degraded_draft
    )
    assert "evidence_degraded" in degraded.executed_checks

    degraded_without_hits = EvidenceFinding(
        query_digest=QUERY_DIGEST,
        status="degraded",
        hits=(),
        degradation=("reranker_timeout",),
        checked=("published_knowledge_scope",),
    )
    with pytest.raises(solution.SolutionContractViolation) as error:
        await solution.ProposalSolutionAnalysis().analyse(
            (requirement(),),
            degraded_without_hits,
            draft_with(
                solution,
                statements=(fact(solution, "产品支持私有部署", uuid4()),),
            ),
        )
    assert error.value.code == "solution_contract_failed"


@pytest.mark.asyncio
async def test_missing_key_structure_fails_instead_of_filling_blanks():
    solution = solution_node()
    evidence = finding()
    evidence_id = evidence.hits[0].evidence_id
    statements = (fact(solution, "产品支持私有部署", evidence_id),)
    incomplete = (
        draft_with(solution, statements=statements, recommended=None),
        draft_with(solution, statements=statements, recommended="   "),
        draft_with(solution, statements=statements, deployment_integration=()),
        draft_with(solution, statements=statements, assumptions=()),
        draft_with(solution, statements=statements, limitations=()),
    )
    for draft in incomplete:
        with pytest.raises(solution.SolutionStructureMissing) as error:
            await solution.ProposalSolutionAnalysis().analyse((requirement(),), evidence, draft)
        assert error.value.code == "solution_structure_missing"

    complete = draft_with(solution, statements=statements)
    result = await solution.ProposalSolutionAnalysis().analyse((requirement(),), evidence, complete)
    assert result.recommended == complete.recommended
    assert result.deployment_integration == complete.deployment_integration
    assert all(part.strip() for part in result.assumptions)
    assert all(part.strip() for part in result.limitations)


@pytest.mark.asyncio
async def test_exhausted_budget_fails_instead_of_truncating():
    solution = solution_node()
    evidence = finding()
    evidence_id = evidence.hits[0].evidence_id
    long_text = "需要支持跨地域双活部署与审计留存的详细要求" * 40
    oversized = requirement("req-1", long_text)
    draft = draft_with(
        solution,
        statements=(fact(solution, "产品支持私有部署", evidence_id),),
    )
    node = solution.ProposalSolutionAnalysis(max_characters=120)

    with pytest.raises(solution.SolutionCapacityExceeded) as error:
        await node.analyse((oversized,), evidence, draft)
    assert error.value.code == "solution_capacity_exceeded"
    assert oversized.text == long_text
    assert len(draft.statements) == 1

    with pytest.raises(solution.SolutionCapacityExceeded):
        await node.analyse((oversized,), evidence, draft)

    roomy = solution.ProposalSolutionAnalysis(max_characters=60_000)
    result = await roomy.analyse((oversized,), evidence, draft)
    assert result.recommended == draft.recommended
    assert result.statements[0].text == "产品支持私有部署"
