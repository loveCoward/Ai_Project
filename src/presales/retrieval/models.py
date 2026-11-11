from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from presales.domain.contracts import SourceDeclaration
from presales.knowledge.models import Applicability
from presales.materials.models import Location


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    scope: Applicability
    declaration: SourceDeclaration
    source_scope: Literal["published_knowledge"] = "published_knowledge"

    @field_validator("query")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A query is required")
        return value.strip()

    @field_validator("declaration")
    @classmethod
    def permitted_data(cls, value: SourceDeclaration) -> SourceDeclaration:
        if value not in {
            SourceDeclaration.SYNTHETIC,
            SourceDeclaration.OWNED,
            SourceDeclaration.PUBLIC_AUTHORIZED,
        }:
            raise ValueError("A permitted source declaration is required")
        return value


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["restricted-dual-v1"] = "restricted-dual-v1"
    tokenizer: Literal["unicode-cjk-word-v1"] = "unicode-cjk-word-v1"
    per_route: int = Field(default=20, ge=1, le=20)
    limit: int = Field(default=8, ge=1, le=8)
    rrf_constant: int = Field(default=60, ge=1, le=1000)
    embedding_seconds: float = Field(default=60, gt=0, le=60)
    reranker_seconds: float = Field(default=3, gt=0, le=3)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: UUID
    source_version_id: UUID
    build_id: UUID
    location: Location
    start_offset: int
    end_offset: int
    text: str
    score: float | None = None


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    mode: Literal["fake", "real"]
    model_identity: str
    authorization_revision: int
    catalog_revision: int
    cache_hit: bool
    degradation: list[str]
    hits: list[Evidence]
