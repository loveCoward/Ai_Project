import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DomainError(Exception):
    code = "domain_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class AccessDenied(DomainError):
    code = "not_found_or_not_allowed"


class AuthenticationFailed(DomainError):
    code = "authentication_failed"


class Conflict(DomainError):
    code = "revision_or_request_conflict"


class InvalidInput(DomainError):
    code = "invalid_input"


class NotReady(DomainError):
    code = "not_ready"


class StorageUnavailable(DomainError):
    code = "storage_unavailable_reconcile_request"


class Role(StrEnum):
    SALES = "sales"
    ARCHITECT = "architect"
    REVIEWER = "reviewer"


class TaskStatus(StrEnum):
    NEW = "new"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed_awaiting_submission"
    REVIEW = "awaiting_review"
    SUPPLEMENT = "awaiting_supplementation"
    APPROVED = "approved"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    REVIEW = "awaiting_review"
    CHANGES = "changes_requested"
    REJECTED = "rejected"
    APPROVED = "approved"
    RETIRED = "retired"


class SourceDeclaration(StrEnum):
    SYNTHETIC = "synthetic"
    PUBLIC_AUTHORIZED = "public_authorized"
    OWNED = "owned"
    UNKNOWN = "unknown"
    SENSITIVE = "real_customer_sensitive"


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    subject: UUID


@dataclass(frozen=True)
class PrincipalStatus:
    subject: UUID
    role: Role
    active: bool


class DraftContent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "$comment": "Stage-one editable draft storage input only; not the complete P06 Proposal Package contract."
        },
    )
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=300_000)
    evidence_version_ids: list[UUID] = Field(default_factory=list, max_length=1000)


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
