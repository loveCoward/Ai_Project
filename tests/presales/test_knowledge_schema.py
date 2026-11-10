import pytest
from pydantic import ValidationError

from presales.knowledge.models import Applicability


def test_applicability_overlap_is_explicit_and_branch_specific():
    scope = Applicability(products=("p1",), deployments=("private",))
    assert scope.overlaps(Applicability(products=("p1",), deployments=("*",)))
    assert not scope.overlaps(Applicability(products=("p2",), deployments=("private",)))
    assert not scope.overlaps(Applicability(products=("p1",), deployments=("cloud",)))
    with pytest.raises(ValidationError):
        Applicability(products=("*", "p1"), deployments=("private",))
    with pytest.raises(ValidationError):
        Applicability(products=("p1",), deployments=())
