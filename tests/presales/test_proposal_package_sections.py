from uuid import uuid4

import pytest
from pydantic import ValidationError

from presales.domain.proposal_package_sections import (
    CoverageBlock,
    EvidenceLocation,
    RequirementItem,
)


@pytest.mark.parametrize(
    "locator",
    [
        {"kind": "pdf_page"},
        {"kind": "markdown_paragraph", "paragraph": 0},
        {"kind": "json_path", "page": 1, "path": "$.items[0]"},
        {"kind": "table_cell", "table": "synthetic", "row": 1},
        {"kind": "pdf_page", "page": 1, "start_offset": 10, "end_offset": 2},
    ],
)
def test_invalid_source_locations_do_not_create_resolvable_evidence(locator):
    with pytest.raises(ValidationError):
        EvidenceLocation.model_validate(locator)


def test_outside_product_scope_remains_in_coverage_denominator():
    requirement = RequirementItem(
        id="REQ-1",
        text="Synthetic unsupported mandatory requirement",
        source_evidence_ids=(uuid4(),),
        importance="mandatory",
        scope_status="confirmed_in_scope",
        coverage_status="out_of_scope",
    )
    coverage = CoverageBlock.from_requirements(uuid4(), [requirement])
    assert coverage.total_requirements == 1
    assert coverage.fully_covered == 0
    assert coverage.business_ratio == 0
    assert coverage.counts.out_of_scope == 1


def test_fully_covered_requirement_needs_supporting_evidence():
    with pytest.raises(ValidationError, match="covered requirements require evidence"):
        RequirementItem(
            id="REQ-1",
            text="Synthetic unsupported claim",
            source_evidence_ids=(uuid4(),),
            importance="mandatory",
            scope_status="confirmed_in_scope",
            coverage_status="covered",
        )
