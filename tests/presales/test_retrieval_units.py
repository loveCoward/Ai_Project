from uuid import UUID

import pytest
from pydantic import ValidationError

from presales.indexing.chunks import tokens
from presales.retrieval.models import EvidenceQuery, RetrievalConfig
from presales.retrieval.ranking import bm25, rrf


def query(**overrides):
    return EvidenceQuery.model_validate(
        {
            "query": "私有部署审计",
            "declaration": "synthetic",
            "scope": {"products": ["synthetic-product"], "deployments": ["private"]},
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor", "forged"),
        ("analysis_attempt_id", "forged"),
        ("grant", "all"),
        ("endpoint", "https://invalid.example"),
        ("source_scope", "all"),
        ("query", "  "),
        ("query", "x" * 2001),
        ("declaration", "unknown"),
        ("declaration", "real_customer_sensitive"),
    ],
)
def test_query_rejects_untrusted_fields_and_invalid_inputs(field, value):
    with pytest.raises(ValidationError):
        query(**{field: value})


def test_bm25_cjk_and_rrf_stable_ties():
    a, b = UUID(int=1), UUID(int=2)
    documents = {a: tokens("私有部署审计"), b: tokens("unrelated")}
    assert bm25("审计", documents)[0][0] == a
    assert bm25("absent", documents) == []
    assert [i for i, _ in rrf([[a, b], [b, a]])] == [a, b]
    assert rrf([[a, a]]) == rrf([[a]])
    assert rrf([]) == []
    assert query().source_scope == "published_knowledge"


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 9}, {"per_route": 21}, {"reranker_seconds": 4}, {"tokenizer": "unversioned"}],
)
def test_retrieval_configuration_cannot_silently_expand_limits(kwargs):
    with pytest.raises(ValidationError):
        RetrievalConfig(**kwargs)
