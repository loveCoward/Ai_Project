from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from presales.domain.contracts import SourceDeclaration
from presales.domain.proposal_analysis_attempt import ModelMode
from presales.domain.proposal_package import (
    REQUIRED_BLOCKS,
    CoverageBlock,
    ProposalPackage,
    RequirementItem,
    adapt_legacy_draft,
    proposal_package_json_schema,
)


def package_payload() -> dict:
    opportunity = uuid4()
    customer = uuid4()
    task = uuid4()
    attempt = uuid4()
    version = uuid4()
    source = uuid4()
    result_revision = uuid4()
    before_revision = uuid4()
    evidence_ids = [uuid4() for _ in range(10)]
    clarification = "CLARIFY-1"
    evidence = [
        {
            "id": item,
            "source_version_id": source,
            "source_type": "product",
            "declaration": SourceDeclaration.SYNTHETIC.value,
            "location": {"kind": "markdown_paragraph", "paragraph": index + 1},
            "fragment_sha256": "a" * 64,
            "excerpt": f"synthetic evidence {index}",
            "applicability": {
                "product_scope": ["synthetic-product"],
                "deployment_scope": ["private"],
            },
            "authorization_revision": 1,
        }
        for index, item in enumerate(evidence_ids)
    ]
    requirements = []
    statuses = [
        "covered",
        "covered",
        "covered",
        "covered",
        "covered",
        "covered",
        "partially_covered",
        "partially_covered",
        "clarification_needed",
        "out_of_scope",
    ]
    for index, status in enumerate(statuses):
        requirements.append(
            {
                "id": f"REQ-{index + 1}",
                "text": f"Synthetic requirement {index + 1}",
                "source_evidence_ids": [evidence_ids[index]],
                "importance": "mandatory" if index < 3 else "optional",
                "scope_status": "confirmed_in_scope",
                "coverage_status": status,
                "evidence_ids": (
                    [evidence_ids[index]] if status in {"covered", "partially_covered"} else []
                ),
                "risk_ids": [],
                "clarification_ids": [clarification] if status == "clarification_needed" else [],
            }
        )
    coverage = CoverageBlock.from_requirements(
        uuid4(),
        [RequirementItem.model_validate(item) for item in requirements],
    )
    return {
        "identity": {
            "schema_version": "1.0",
            "customer_id": customer,
            "opportunity_id": opportunity,
            "task_id": task,
            "attempt_id": attempt,
            "proposal_version_id": version,
            "content_revision": 1,
        },
        "provenance": {
            "inputs": [
                {
                    "source_version_id": source,
                    "content_digest": "b" * 64,
                    "source_kind": "customer_material",
                }
            ],
            "model_mode": ModelMode.FAKE.value,
            "model_identity": "fake-model",
            "endpoint_identity": "local-fake",
            "template_version": "analysis-v1",
            "config_identity": "config-v1",
            "generated_at": datetime.now(UTC),
        },
        "access": {
            "owner_subject_id": uuid4(),
            "direct_source_version_ids": [source],
            "derived_artifact_scope": ["task"],
            "restricted_read_note": "task-scoped derived artifact",
        },
        "requirements": requirements,
        "coverage": coverage.model_dump(mode="json"),
        "evidence": evidence,
        "solution": {
            "recommended": "Synthetic solution",
            "deployment_integration": ["private deployment"],
            "assumptions": ["synthetic inputs"],
            "limitations": ["not a production commitment"],
            "delivery_notes": ["requires customer confirmation"],
            "commitments": [
                {
                    "kind": "schedule",
                    "text": "Target date is pending confirmation",
                    "binding": False,
                    "confirmation_required": True,
                }
            ],
        },
        "risks": [
            {
                "id": "RISK-1",
                "category": "delivery",
                "severity": "low",
                "basis": "Synthetic delivery note",
                "impact": "Synthetic impact",
                "mitigation": "Synthetic mitigation",
                "owner": "synthetic-owner",
                "disposition": "pending",
                "evidence_ids": [evidence_ids[0]],
            }
        ],
        "clarifications": [
            {
                "id": clarification,
                "question": "Synthetic clarification needed?",
                "requirement_ids": ["REQ-9"],
                "critical": True,
                "status": "open",
                "answer_evidence_ids": [],
            }
        ],
        "review": {"status": "not_submitted", "records": []},
        "approval": {"status": "not_decided", "records": [], "approved_action_set": None},
        "execution": {
            "nodes": [
                {
                    "name": "requirements",
                    "status": "succeeded",
                    "output_revision_id": before_revision,
                }
            ],
            "degradations": [],
            "budget": {
                "active_seconds": 12,
                "model_calls": 4,
                "mcp_calls": 1,
                "technical_retries": 0,
                "reflection_revision": 1,
            },
            "reflection": {
                "status": "completed",
                "before_revision_id": before_revision,
                "after_revision_id": result_revision,
                "changes": ["synthetic clarification added"],
            },
            "result_ready": True,
            "result_revision_id": result_revision,
            "external_execution": "not_executed",
            "publication": "not_executed",
        },
        "memory_updates": [
            {
                "statement": "Synthetic candidate statement",
                "source_evidence_ids": [evidence_ids[0]],
                "scope": ["synthetic-product"],
                "trust_status": "pending_confirmation",
                "write_status": "not_executed",
            }
        ],
    }


def test_schema_1_0_has_all_blocks_and_roundtrips():
    payload = package_payload()
    package = ProposalPackage.model_validate(payload)
    assert all(block in proposal_package_json_schema()["properties"] for block in REQUIRED_BLOCKS)
    assert package.coverage.total_requirements == 10
    assert package.coverage.fully_covered == 6
    assert package.coverage.business_ratio == 0.6
    assert package.review.status == "not_submitted"
    assert package.approval.status == "not_decided"
    assert package.execution.publication == "not_executed"
    assert package.memory_updates[0].write_status == "not_executed"
    assert ProposalPackage.from_json(package.to_json()).to_json() == package.to_json()


def test_missing_block_and_broken_reference_are_rejected():
    payload = package_payload()
    missing = dict(payload)
    missing.pop("evidence")
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(missing)

    broken = package_payload()
    broken["requirements"][0]["evidence_ids"] = [uuid4()]
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(broken)


@pytest.mark.parametrize("block", REQUIRED_BLOCKS)
def test_every_business_block_is_required_even_when_empty(block):
    payload = package_payload()
    payload.pop(block)
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)
    assert block in proposal_package_json_schema()["required"]


def test_coverage_is_deterministic_and_empty_scope_is_not_applicable():
    wrong = package_payload()
    wrong["coverage"]["fully_covered"] = 7
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(wrong)

    empty = package_payload()
    empty["requirements"] = []
    empty["coverage"] = CoverageBlock.from_requirements(uuid4(), []).model_dump(mode="json")
    empty["evidence"] = []
    empty["risks"] = []
    empty["clarifications"] = []
    empty["memory_updates"] = []
    package = ProposalPackage.model_validate(empty)
    assert package.coverage.applicable is False
    assert package.coverage.business_ratio is None


def test_analysis_cannot_claim_review_approval_publication_or_binding_commitment():
    payload = package_payload()
    payload["review"]["records"] = [{"approved": True}]
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)

    payload = package_payload()
    payload["approval"]["records"] = [{"approved": True}]
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)

    payload = package_payload()
    payload["execution"]["publication"] = "published"
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)

    payload = package_payload()
    payload["memory_updates"][0]["write_status"] = "written"
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)

    payload = package_payload()
    payload["solution"]["commitments"][0]["binding"] = True
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)


def test_legacy_draft_cannot_pass_complete_package_validation():
    legacy = {
        "schema_version": "1.0",
        "identity": {"analysis_attempt_id": None, "task_id": str(uuid4())},
        "provenance": {"mode": "manual_draft"},
        "access": {"direct_source_version_ids": []},
        "draft": {
            "schema_version": "1.0",
            "title": "Manual",
            "notes": "",
            "evidence_version_ids": [],
        },
        "incomplete_sections": ["requirements", "coverage", "solution"],
    }
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(legacy)
    adapted = adapt_legacy_draft(legacy)
    assert adapted.complete is False
    assert adapted.schema_version == "1.0"
    assert adapted.missing_sections == ("requirements", "coverage", "solution")
    with pytest.raises(ValueError):
        ProposalPackage.from_json(legacy)


def test_risk_and_clarification_gates_are_not_self_approved():
    payload = package_payload()
    payload["risks"][0]["severity"] = "severe"
    payload["risks"][0]["disposition"] = "accepted"
    payload["risks"][0]["decision"] = {
        "decision_id": str(uuid4()),
        "decided_by": str(uuid4()),
        "decided_at": datetime.now(UTC).isoformat(),
        "action_scope": "synthetic",
    }
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)

    payload = package_payload()
    payload["risks"][0]["severity"] = "high"
    payload["risks"][0]["disposition"] = "accepted"
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)

    payload = package_payload()
    payload["clarifications"][0]["status"] = "resolved"
    payload["clarifications"][0]["answer"] = "Synthetic answer"
    with pytest.raises(ValidationError):
        ProposalPackage.model_validate(payload)
