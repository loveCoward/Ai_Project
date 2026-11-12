"""Evidence node: reads published knowledge and task materials through 2A ports only."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from presales.domain.contracts import AccessDenied, Actor, DomainError, NotReady, digest
from presales.knowledge.models import Applicability
from presales.retrieval.models import Evidence, EvidenceQuery, RetrievalResult

EvidenceOutcomeStatus = Literal[
    "content",
    "empty",
    "not_found_or_not_allowed",
    "conflict",
    "unavailable",
    "degraded",
]

UsableDeclaration = Literal["synthetic", "owned", "public_authorized"]


class EvidenceNodeFailure(DomainError):
    code = "evidence_node_failed"


class EvidenceContractViolation(DomainError):
    code = "evidence_contract_failed"


class EvidenceHit(BaseModel):
    """Evidence kept verbatim from the 2A port; nothing is synthesised here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID
    source_version_id: UUID
    declaration: UsableDeclaration
    applicability: Applicability
    locator: dict[str, Any] = Field(default_factory=dict)
    text: str = Field(min_length=1, max_length=20_000)
    score: float | None = None
    origin: Literal["knowledge", "catalog", "material"]


class EvidenceFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: EvidenceOutcomeStatus
    hits: tuple[EvidenceHit, ...] = ()
    degradation: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=100)
    checked: tuple[str, ...] = ()

    def require_usable(self) -> "EvidenceFinding":
        if self.status in {"unavailable", "conflict", "not_found_or_not_allowed"}:
            raise EvidenceNodeFailure()
        return self


class RetrievalPort(Protocol):
    async def retrieve_evidence(self, actor: Actor, query: EvidenceQuery) -> RetrievalResult: ...


class EvidenceReader(Protocol):
    async def read_evidence(self, actor: Actor, evidence_id: UUID) -> Evidence: ...


class CatalogPort(Protocol):
    async def lookup(self, actor: Actor, query: EvidenceQuery) -> Any: ...


class MaterialPort(Protocol):
    async def inputs(self, actor: Actor, task_id: UUID) -> dict[str, Any]: ...


@dataclass(frozen=True)
class EvidencePorts:
    retrieval: RetrievalPort
    reader: EvidenceReader
    catalog: CatalogPort | None = None
    materials: MaterialPort | None = None


class EvidenceAnalysis:
    """Asks the 2A ports for evidence and records exactly what came back."""

    def __init__(
        self,
        ports: EvidencePorts,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.ports = ports
        self.clock = clock

    async def analyse(
        self,
        actor: Actor,
        query: EvidenceQuery,
        *,
        task_id: UUID | None = None,
        use_catalog: bool = True,
    ) -> EvidenceFinding:
        findings: list[EvidenceHit] = []
        degradation: list[str] = []
        checks: list[str] = ["published_knowledge_scope"]
        try:
            knowledge = await self.ports.retrieval.retrieve_evidence(actor, query)
        except AccessDenied:
            return EvidenceFinding(
                query_digest=self._digest(query),
                status="not_found_or_not_allowed",
                error_code="not_found_or_not_allowed",
                checked=tuple(checks),
            )
        except NotReady:
            return EvidenceFinding(
                query_digest=self._digest(query),
                status="unavailable",
                error_code="not_ready",
                checked=tuple(checks),
            )
        except DomainError as error:
            conflict = error.code == "revision_or_request_conflict"
            return EvidenceFinding(
                query_digest=self._digest(query),
                status="conflict" if conflict else "unavailable",
                error_code=error.code,
                checked=tuple(checks),
            )
        degradation.extend(knowledge.degradation)
        if knowledge.hits:
            findings.extend(self._hit(hit, query, origin="knowledge") for hit in knowledge.hits)

        if use_catalog and self.ports.catalog is not None:
            checks.append("published_catalog_scope")
            try:
                catalog = await self.ports.catalog.lookup(actor, query)
            except DomainError as error:
                degradation.append(
                    "catalog_not_allowed"
                    if error.code == "not_found_or_not_allowed"
                    else "catalog_unavailable"
                )
            else:
                status = getattr(catalog, "status", None)
                if status == "unavailable":
                    degradation.append("catalog_unavailable")
                elif status == "not_found_or_not_allowed":
                    degradation.append("catalog_not_allowed")
                elif status == "ok" and getattr(catalog, "result", None) is not None:
                    findings.extend(
                        self._hit(hit, query, origin="catalog") for hit in catalog.result.hits
                    )

        if task_id is not None and self.ports.materials is not None:
            checks.append("task_material_manifest")
            try:
                material = await self.ports.materials.inputs(actor, task_id)
            except AccessDenied:
                return EvidenceFinding(
                    query_digest=self._digest(query),
                    status="not_found_or_not_allowed",
                    error_code="task_material_not_found_or_not_allowed",
                    checked=tuple(checks),
                )
            except NotReady:
                return EvidenceFinding(
                    query_digest=self._digest(query),
                    status="unavailable",
                    error_code="task_material_not_ready",
                    checked=tuple(checks),
                )
            except DomainError as error:
                conflict = error.code == "revision_or_request_conflict"
                return EvidenceFinding(
                    query_digest=self._digest(query),
                    status="conflict" if conflict else "unavailable",
                    error_code=error.code,
                    checked=tuple(checks),
                )
            unresolved = self._unresolved_materials(material)
            if unresolved:
                degradation.append("task_material_not_ready")

        findings = self._deduplicate(findings)
        if not findings:
            degradation_only = tuple(sorted(set(degradation)))
            return EvidenceFinding(
                query_digest=self._digest(query),
                status="degraded" if degradation_only else "empty",
                degradation=degradation_only,
                checked=tuple(checks),
            )
        return EvidenceFinding(
            query_digest=self._digest(query),
            status="content",
            hits=tuple(findings),
            degradation=tuple(sorted(set(degradation))),
            checked=tuple(checks),
        )

    async def verify(self, actor: Actor, finding: EvidenceFinding) -> EvidenceFinding:
        """Re-reads every cited evidence id; a missing citation never stays 'content'."""
        for hit in finding.hits:
            if hit.origin == "knowledge":
                try:
                    reread = await self.ports.reader.read_evidence(actor, hit.evidence_id)
                except (AccessDenied, NotReady, DomainError):
                    return EvidenceFinding(
                        query_digest=finding.query_digest,
                        status="not_found_or_not_allowed",
                        error_code="evidence_unavailable",
                        checked=finding.checked + ("evidence_reread",),
                    )
                if reread.source_version_id != hit.source_version_id:
                    raise EvidenceContractViolation()
        return finding

    def _hit(
        self,
        hit: Evidence,
        query: EvidenceQuery,
        *,
        origin: Literal["knowledge", "catalog", "material"],
    ) -> EvidenceHit:
        if hit.text.strip() == "":
            raise EvidenceContractViolation()
        declaration = hit.declaration or query.declaration
        if declaration.value not in {"synthetic", "owned", "public_authorized"}:
            raise EvidenceContractViolation()
        try:
            return EvidenceHit(
                evidence_id=hit.evidence_id,
                source_version_id=hit.source_version_id,
                declaration=cast(UsableDeclaration, declaration.value),
                applicability=hit.applicability or query.scope,
                locator=hit.location.model_dump(mode="json", exclude_none=True),
                text=hit.text,
                score=hit.score,
                origin=origin,
            )
        except ValidationError:
            raise EvidenceContractViolation() from None

    @staticmethod
    def _deduplicate(hits: Iterable[EvidenceHit]) -> list[EvidenceHit]:
        seen: dict[tuple[str, UUID], EvidenceHit] = {}
        for hit in hits:
            seen.setdefault((hit.origin, hit.evidence_id), hit)
        return list(seen.values())

    @staticmethod
    def _unresolved_materials(material: dict[str, Any]) -> list[Any]:
        """Reads the 2A input status shape; an unrecognised shape is a contract failure."""
        if "unresolved" in material or "complete" in material:
            listed = material.get("unresolved") or []
            return [item for item in listed if item]
        if "sources" in material or "required" in material:
            listed = material.get("sources") or material.get("required") or []
            return [
                item
                for item in listed
                if isinstance(item, dict) and not item.get("accessible", True)
            ]
        raise EvidenceContractViolation()

    @staticmethod
    def _digest(query: EvidenceQuery) -> str:
        return digest(query.model_dump(mode="json"))


def evidence_sources(finding: EvidenceFinding) -> Sequence[tuple[UUID, str]]:
    """Source versions actually used, for Evidence Snapshot registration."""
    return tuple(dict.fromkeys((hit.source_version_id, hit.origin) for hit in finding.hits))


__all__ = [
    "CatalogPort",
    "EvidenceAnalysis",
    "EvidenceContractViolation",
    "EvidenceFinding",
    "EvidenceHit",
    "EvidenceNodeFailure",
    "EvidencePorts",
    "MaterialPort",
    "RetrievalPort",
    "evidence_sources",
]
