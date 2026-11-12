"""Offline contracts for the evidence node; 2A ports are faked, no召回率 claim is made."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from presales.application.evidence_analysis import (
    EvidenceAnalysis,
    EvidenceContractViolation,
    EvidenceNodeFailure,
    EvidencePorts,
    evidence_sources,
)
from presales.domain.contracts import AccessDenied, Actor, Conflict, NotReady
from presales.knowledge.models import Applicability
from presales.materials.models import Location
from presales.retrieval.models import Evidence, EvidenceQuery, RetrievalResult

SCOPE = Applicability(products=("synthetic-product",), deployments=("private",))


def query(text="私有部署审计") -> EvidenceQuery:
    return EvidenceQuery(query=text, scope=SCOPE, declaration="synthetic")


def evidence(
    *,
    text="私有部署支持审计日志",
    score=0.9,
    version=None,
    declaration=None,
    applicability=None,
) -> Evidence:
    return Evidence(
        evidence_id=uuid4(),
        source_version_id=version or uuid4(),
        build_id=uuid4(),
        location=Location(kind="pdf_page", page=3),
        start_offset=0,
        end_offset=len(text),
        text=text,
        score=score,
        declaration=declaration,
        applicability=applicability,
    )


def result(hits=(), *, degradation=(), mode="fake") -> RetrievalResult:
    return RetrievalResult(
        run_id=uuid4(),
        mode=mode,
        model_identity="fake-embedding",
        authorization_revision=1,
        catalog_revision=1,
        cache_hit=False,
        degradation=list(degradation),
        hits=list(hits),
    )


class Retrieval:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple] = []

    async def retrieve_evidence(self, actor, request):
        self.calls.append((actor.subject, request.query))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class Reader:
    def __init__(self, *, fail=False, version=None) -> None:
        self.fail, self.version = fail, version
        self.calls: list = []

    async def read_evidence(self, actor, evidence_id):
        self.calls.append(evidence_id)
        if self.fail:
            raise AccessDenied()
        return evidence(version=self.version)


class Catalog:
    def __init__(self, status, hits=()) -> None:
        self.status, self.hits = status, hits
        self.calls: list = []

    async def lookup(self, actor, request):
        self.calls.append((actor.subject, request.query))

        class Response:
            def __init__(self) -> None:
                self.status = self_status
                self.result = result(self_hits) if self_status == "ok" else None

        self_status, self_hits = self.status, list(self.hits)
        return Response()


class Materials:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list = []

    async def inputs(self, actor, task_id):
        self.calls.append(task_id)
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def analysis(*, retrieval=None, reader=None, catalog=None, materials=None) -> EvidenceAnalysis:
    return EvidenceAnalysis(
        EvidencePorts(
            retrieval=retrieval or Retrieval(result()),
            reader=reader or Reader(),
            catalog=catalog,
            materials=materials,
        )
    )


@pytest.mark.asyncio
async def test_evidence_keeps_version_locator_and_applicability():
    hit = evidence()
    subject = Actor(subject=uuid4())
    finding = await analysis(retrieval=Retrieval(result([hit]))).analyse(subject, query())
    assert finding.status == "content"
    assert finding.hits[0].evidence_id == hit.evidence_id
    assert finding.hits[0].source_version_id == hit.source_version_id
    assert finding.hits[0].declaration == "synthetic"
    assert finding.hits[0].applicability == SCOPE
    assert finding.hits[0].locator == {"kind": "pdf_page", "page": 3, "heading": []}
    assert finding.hits[0].origin == "knowledge"
    assert finding.checked == ("published_knowledge_scope",)


@pytest.mark.asyncio
async def test_empty_result_is_distinct_from_failure():
    subject = Actor(subject=uuid4())
    empty = await analysis(retrieval=Retrieval(result())).analyse(subject, query())
    assert empty.status == "empty"
    assert empty.hits == () and empty.error_code is None
    denied = await analysis(retrieval=Retrieval(AccessDenied())).analyse(subject, query())
    assert denied.status == "not_found_or_not_allowed"
    assert denied.error_code == "not_found_or_not_allowed"


@pytest.mark.asyncio
async def test_not_ready_and_conflict_are_reported_apart():
    subject = Actor(subject=uuid4())
    not_ready = await analysis(retrieval=Retrieval(NotReady())).analyse(subject, query())
    assert not_ready.status == "unavailable"
    assert not_ready.error_code == "not_ready"
    conflict = await analysis(retrieval=Retrieval(Conflict())).analyse(subject, query())
    assert conflict.status == "conflict"
    assert conflict.error_code == "revision_or_request_conflict"


@pytest.mark.asyncio
async def test_degradation_is_passed_through_verbatim():
    subject = Actor(subject=uuid4())
    finding = await analysis(
        retrieval=Retrieval(
            result([evidence()], degradation=["reranker_failed_rrf_retained", "cache_miss"])
        )
    ).analyse(subject, query())
    assert finding.status == "content"
    assert "reranker_failed_rrf_retained" in finding.degradation
    assert "cache_miss" in finding.degradation


@pytest.mark.asyncio
async def test_version_metadata_wins_over_the_query_scope():
    subject = Actor(subject=uuid4())
    version_scope = Applicability(products=("other-product",), deployments=("saas",))
    finding = await analysis(
        retrieval=Retrieval(
            result(
                [
                    evidence(
                        declaration="public_authorized",
                        applicability=version_scope,
                    )
                ]
            )
        )
    ).analyse(subject, query())
    hit = finding.hits[0]
    assert hit.declaration == "public_authorized"
    assert hit.applicability == version_scope
    assert hit.applicability != SCOPE


@pytest.mark.asyncio
async def test_query_scope_is_only_the_fallback_when_the_port_omits_it():
    subject = Actor(subject=uuid4())
    finding = await analysis(retrieval=Retrieval(result([evidence()]))).analyse(subject, query())
    hit = finding.hits[0]
    assert hit.declaration == "synthetic"
    assert hit.applicability == SCOPE


@pytest.mark.asyncio
async def test_degraded_only_result_is_not_reported_as_content():
    subject = Actor(subject=uuid4())
    finding = await analysis(
        retrieval=Retrieval(result(degradation=["embedding_failed_keyword_only"]))
    ).analyse(subject, query())
    assert finding.status == "degraded"
    assert finding.hits == ()
    assert "embedding_failed_keyword_only" in finding.degradation


@pytest.mark.asyncio
async def test_catalog_output_is_labelled_and_isolated():
    subject = Actor(subject=uuid4())
    catalog_hit = evidence(text="产品能力说明")
    finding = await analysis(
        retrieval=Retrieval(result()),
        catalog=Catalog("ok", [catalog_hit]),
    ).analyse(subject, query())
    assert finding.status == "content"
    assert [hit.origin for hit in finding.hits] == ["catalog"]
    assert "published_catalog_scope" in finding.checked


@pytest.mark.asyncio
async def test_catalog_failure_degrades_instead_of_raising():
    subject = Actor(subject=uuid4())
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])),
        catalog=Catalog("unavailable"),
    ).analyse(subject, query())
    assert finding.status == "content"
    assert "catalog_unavailable" in finding.degradation
    denied = await analysis(
        retrieval=Retrieval(result([evidence()])), catalog=Catalog("not_found_or_not_allowed")
    ).analyse(subject, query())
    assert "catalog_not_allowed" in denied.degradation


@pytest.mark.asyncio
async def test_catalog_is_not_consulted_when_disabled():
    subject = Actor(subject=uuid4())
    catalog = Catalog("ok", [evidence()])
    finding = await analysis(retrieval=Retrieval(result([evidence()])), catalog=catalog).analyse(
        subject, query(), use_catalog=False
    )
    assert catalog.calls == []
    assert "published_catalog_scope" not in finding.checked


@pytest.mark.asyncio
async def test_successful_but_empty_catalog_is_not_a_failure():
    subject = Actor(subject=uuid4())
    finding = await analysis(retrieval=Retrieval(result()), catalog=Catalog("ok", [])).analyse(
        subject, query()
    )
    assert finding.status == "empty"
    assert finding.error_code is None
    assert finding.degradation == ()
    assert "published_catalog_scope" in finding.checked


@pytest.mark.asyncio
async def test_materials_are_not_consulted_without_a_task():
    subject = Actor(subject=uuid4())
    materials = Materials({"complete": False, "unresolved": [str(uuid4())]})
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), materials=materials
    ).analyse(subject, query())
    assert materials.calls == []
    assert "task_material_manifest" not in finding.checked
    assert "task_material_not_ready" not in finding.degradation


@pytest.mark.asyncio
async def test_unresolved_task_material_is_marked_degraded():
    subject = Actor(subject=uuid4())
    materials = Materials({"complete": False, "unresolved": [str(uuid4())]})
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), materials=materials
    ).analyse(subject, query(), task_id=uuid4())
    assert "task_material_not_ready" in finding.degradation
    assert "task_material_manifest" in finding.checked
    assert len(materials.calls) == 1


@pytest.mark.asyncio
async def test_resolved_task_material_does_not_degrade():
    subject = Actor(subject=uuid4())
    materials = Materials({"complete": True, "unresolved": [], "unused_sources": []})
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), materials=materials
    ).analyse(subject, query(), task_id=uuid4())
    assert finding.status == "content"
    assert finding.degradation == ()
    assert "task_material_manifest" in finding.checked
    assert len(materials.calls) == 1


@pytest.mark.asyncio
async def test_legacy_accessible_flag_payload_is_still_honoured():
    subject = Actor(subject=uuid4())
    materials = Materials({"sources": [{"version_id": str(uuid4()), "accessible": False}]})
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), materials=materials
    ).analyse(subject, query(), task_id=uuid4())
    assert "task_material_not_ready" in finding.degradation


@pytest.mark.asyncio
async def test_task_material_denial_is_reported_without_leaking_text():
    subject = Actor(subject=uuid4())
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), materials=Materials(AccessDenied())
    ).analyse(subject, query(), task_id=uuid4())
    assert finding.status == "not_found_or_not_allowed"
    assert finding.error_code == "task_material_not_found_or_not_allowed"
    assert finding.hits == ()
    assert "task_material_manifest" in finding.checked


@pytest.mark.asyncio
async def test_task_material_not_ready_is_reported_as_unavailable():
    subject = Actor(subject=uuid4())
    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), materials=Materials(NotReady())
    ).analyse(subject, query(), task_id=uuid4())
    assert finding.status == "unavailable"
    assert finding.error_code == "task_material_not_ready"
    assert finding.hits == ()


@pytest.mark.asyncio
async def test_unrecognised_material_payload_is_a_contract_violation():
    subject = Actor(subject=uuid4())
    with pytest.raises(EvidenceContractViolation) as error:
        await analysis(
            retrieval=Retrieval(result([evidence()])),
            materials=Materials({"task_revision": 1}),
        ).analyse(subject, query(), task_id=uuid4())
    assert error.value.code == "evidence_contract_failed"


@pytest.mark.asyncio
async def test_duplicate_hits_from_both_ports_are_collapsed():
    subject = Actor(subject=uuid4())
    shared = evidence()
    finding = await analysis(
        retrieval=Retrieval(result([shared])), catalog=Catalog("ok", [shared])
    ).analyse(subject, query())
    assert len(finding.hits) == 2
    assert {hit.origin for hit in finding.hits} == {"knowledge", "catalog"}


@pytest.mark.asyncio
async def test_catalog_exception_degrades_instead_of_escaping():
    subject = Actor(subject=uuid4())

    class RaisingCatalog:
        async def lookup(self, actor, request):
            raise AccessDenied()

    finding = await analysis(
        retrieval=Retrieval(result([evidence()])), catalog=RaisingCatalog()
    ).analyse(subject, query())
    assert finding.status == "content"
    assert "catalog_not_allowed" in finding.degradation
    assert [hit.origin for hit in finding.hits] == ["knowledge"]


@pytest.mark.asyncio
async def test_blank_evidence_text_is_a_contract_violation():
    subject = Actor(subject=uuid4())
    blank = Evidence(
        evidence_id=uuid4(),
        source_version_id=uuid4(),
        build_id=uuid4(),
        location=Location(kind="pdf_page", page=1),
        start_offset=0,
        end_offset=0,
        text="   ",
        score=None,
    )
    with pytest.raises(EvidenceContractViolation) as error:
        await analysis(retrieval=Retrieval(result([blank]))).analyse(subject, query())
    assert error.value.code == "evidence_contract_failed"


@pytest.mark.asyncio
async def test_unpermitted_source_declaration_from_the_port_is_a_contract_violation():
    subject = Actor(subject=uuid4())
    leaked = evidence(declaration="real_customer_sensitive")
    with pytest.raises(EvidenceContractViolation) as error:
        await analysis(retrieval=Retrieval(result([leaked]))).analyse(subject, query())
    assert error.value.code == "evidence_contract_failed"


@pytest.mark.asyncio
async def test_oversized_evidence_text_from_the_port_is_a_contract_violation():
    subject = Actor(subject=uuid4())
    oversized = evidence(text="证据" * 10_001)
    with pytest.raises(EvidenceContractViolation) as error:
        await analysis(retrieval=Retrieval(result([oversized]))).analyse(subject, query())
    assert error.value.code == "evidence_contract_failed"


@pytest.mark.asyncio
async def test_verify_rereads_every_citation_and_fails_when_unreadable():
    subject = Actor(subject=uuid4())
    hit = evidence()
    finding = await analysis(retrieval=Retrieval(result([hit]))).analyse(subject, query())
    verified = await analysis(
        retrieval=Retrieval(result([hit])), reader=Reader(version=hit.source_version_id)
    ).verify(subject, finding)
    assert verified.status == "content"
    failing = analysis(retrieval=Retrieval(result([hit])), reader=Reader(fail=True))
    unavailable = await failing.verify(subject, finding)
    assert unavailable.status == "not_found_or_not_allowed"
    assert unavailable.error_code == "evidence_unavailable"
    assert "evidence_reread" in unavailable.checked


@pytest.mark.asyncio
async def test_verify_detects_a_source_version_swap():
    subject = Actor(subject=uuid4())
    hit = evidence()
    finding = await analysis(retrieval=Retrieval(result([hit]))).analyse(subject, query())
    swapped = analysis(retrieval=Retrieval(result([hit])), reader=Reader(version=uuid4()))
    with pytest.raises(EvidenceContractViolation):
        await swapped.verify(subject, finding)


@pytest.mark.asyncio
async def test_finding_requires_usable_status_before_downstream_use():
    subject = Actor(subject=uuid4())
    denied = await analysis(retrieval=Retrieval(AccessDenied())).analyse(subject, query())
    with pytest.raises(EvidenceNodeFailure) as error:
        denied.require_usable()
    assert error.value.code == "evidence_node_failed"
    ok = await analysis(retrieval=Retrieval(result())).analyse(subject, query())
    assert ok.require_usable() is ok


@pytest.mark.asyncio
async def test_evidence_sources_lists_each_version_once():
    subject = Actor(subject=uuid4())
    version = uuid4()
    finding = await analysis(
        retrieval=Retrieval(result([evidence(version=version), evidence(version=version)]))
    ).analyse(subject, query())
    assert evidence_sources(finding) == ((version, "knowledge"),)


@pytest.mark.asyncio
async def test_query_digest_is_stable_and_matches_the_request():
    from presales.domain.contracts import digest

    subject = Actor(subject=uuid4())
    request = query()
    finding = await analysis(retrieval=Retrieval(result())).analyse(subject, request)
    assert finding.query_digest == digest(request.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_retrieval_port_receives_the_actor_and_query_verbatim():
    subject = Actor(subject=uuid4())
    retrieval = Retrieval(result([evidence()]))
    request = query()
    await analysis(retrieval=retrieval).analyse(subject, request)
    assert retrieval.calls == [(subject.subject, request.query)]


def test_query_rejects_unpermitted_declaration():
    with pytest.raises(ValidationError) as error:
        EvidenceQuery(query="私有部署", scope=SCOPE, declaration="real_customer_sensitive")
    assert "permitted source declaration" in str(error.value)


def test_query_cannot_widen_the_source_scope():
    with pytest.raises(ValidationError) as error:
        EvidenceQuery(
            query="私有部署", scope=SCOPE, declaration="synthetic", source_scope="opportunity"
        )
    assert "source_scope" in str(error.value)
