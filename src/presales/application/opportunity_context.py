"""Read-only opportunity background with locked necessity, bounded retries and traceable sources."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from presales.access.policy import Policy
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    DomainError,
    NotReady,
    StorageUnavailable,
    digest,
)
from presales.domain.proposal_analysis_attempt import BackgroundRequirement
from presales.infrastructure.database import AUTH_LOCK, Database, lock, rows

READ_CONTEXT_SECONDS = 10
READ_CONTEXT_RETRIES = 2
MAX_CONTEXT_CHARACTERS = 60_000
MAX_CONTEXT_RECORDS = 200


class ContextLimits(BaseModel):
    """Configurable background thresholds; defaults match the accepted baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_seconds: float = Field(default=READ_CONTEXT_SECONDS, gt=0, le=600)
    max_characters: int = Field(default=MAX_CONTEXT_CHARACTERS, ge=1, le=1_000_000)
    max_records: int = Field(default=MAX_CONTEXT_RECORDS, ge=1, le=10_000)

    def effective(self) -> dict[str, float | int]:
        return {
            "read_seconds": self.read_seconds,
            "max_characters": self.max_characters,
            "max_records": self.max_records,
        }


class ContextContractViolation(DomainError):
    code = "context_contract_failed"


class ContextUnavailable(DomainError):
    code = "context_unavailable"


class ContextCapacityExceeded(DomainError):
    code = "context_capacity_exceeded"


class BackgroundSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: UUID
    source_version_id: UUID
    kind: Literal["background", "history"]
    title: str
    body: str
    declaration: Literal["synthetic", "owned", "public_authorized"]
    trust_state: Literal["confirmed", "pending_confirmation", "expired", "conflicting"]
    usable_as_fact: bool
    effective_at: datetime


class ContextOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal[
        "content",
        "empty",
        "not_found_or_not_allowed",
        "timeout",
        "unavailable",
        "contract_failed",
        "capacity_exceeded",
    ]
    sources: tuple[BackgroundSource, ...] = ()
    error_code: str | None = Field(default=None, max_length=100)
    degradation: tuple[str, ...] = ()
    character_count: int = Field(default=0, ge=0)


class ContextReader(Protocol):
    async def read(
        self, actor: Actor, opportunity_id: UUID, *, seconds: float
    ) -> ContextOutcome: ...


READ_BACKGROUND_SQL = """
SELECT id,source_version_id,kind,title,body,declaration,trust_state,effective_at
FROM presales.opportunity_background_records
WHERE opportunity_id=:opportunity
ORDER BY effective_at,id
LIMIT :limit
"""

COUNT_BACKGROUND_SQL = """
SELECT coalesce(sum(char_length(body)),0) AS characters,count(*) AS records
FROM presales.opportunity_background_records
WHERE opportunity_id=:opportunity
"""

READ_OPPORTUNITY_SQL = """
SELECT * FROM presales.opportunities WHERE id=:id
"""


class OpportunityContextReader:
    """Reads registered background records only; never trusts caller-supplied identity."""

    def __init__(
        self,
        database: Database,
        *,
        limits: ContextLimits | None = None,
        max_characters: int | None = None,
        max_records: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        base = limits or ContextLimits()
        self.limits = base.model_copy(
            update={
                key: value
                for key, value in (
                    ("max_characters", max_characters),
                    ("max_records", max_records),
                )
                if value is not None
            }
        )
        if self.limits.max_characters < 1 or self.limits.max_records < 1:
            raise ValueError("Context capacity bounds must be positive")
        self.database, self.clock = database, clock

    async def read(self, actor: Actor, opportunity_id: UUID, *, seconds: float) -> ContextOutcome:
        deadline = self.clock().timestamp() + min(seconds, self.limits.read_seconds)
        try:
            return await self._read(actor, opportunity_id, deadline)
        except AccessDenied:
            return ContextOutcome(status="not_found_or_not_allowed")
        except StorageUnavailable:
            return ContextOutcome(status="unavailable", error_code="storage_unavailable")
        except ContextContractViolation as error:
            return ContextOutcome(status="contract_failed", error_code=error.code)
        except DomainError as error:
            return ContextOutcome(status="unavailable", error_code=error.code)

    async def _read(self, actor: Actor, opportunity_id: UUID, deadline: float) -> ContextOutcome:
        async with self.database.transaction() as conn:
            await lock(conn, AUTH_LOCK, shared=True)
            policy = Policy(conn, actor)
            await policy.start()
            opportunities = await rows(conn, READ_OPPORTUNITY_SQL, id=opportunity_id)
            if not opportunities:
                raise AccessDenied()
            try:
                await policy.resource(opportunity_id)
            except AccessDenied:
                raise AccessDenied() from None
            if self.clock().timestamp() >= deadline:
                return ContextOutcome(status="timeout", error_code="context_deadline_exceeded")
            totals = (await rows(conn, COUNT_BACKGROUND_SQL, opportunity=opportunity_id))[0]
            characters, count = int(totals["characters"]), int(totals["records"])
            if characters > self.limits.max_characters or count > self.limits.max_records:
                return ContextOutcome(
                    status="capacity_exceeded",
                    error_code="context_capacity_exceeded",
                    character_count=characters,
                )
            records = await rows(
                conn,
                READ_BACKGROUND_SQL,
                opportunity=opportunity_id,
                limit=self.limits.max_records,
            )
            sources = []
            for record in records:
                version = await rows(
                    conn,
                    "SELECT * FROM presales.resource_versions WHERE id=:id",
                    id=record["source_version_id"],
                )
                if not version:
                    raise ContextContractViolation()
                if version[0]["resource_id"] != opportunity_id:
                    raise ContextContractViolation()
                if version[0]["source_kind"] != "original" or not version[0]["ready"]:
                    raise ContextContractViolation()
                if version[0]["declaration"] != record["declaration"]:
                    raise ContextContractViolation()
                sources.append(
                    BackgroundSource(
                        record_id=record["id"],
                        source_version_id=record["source_version_id"],
                        kind=record["kind"],
                        title=record["title"],
                        body=record["body"],
                        declaration=record["declaration"],
                        trust_state=record["trust_state"],
                        usable_as_fact=record["trust_state"] == "confirmed",
                        effective_at=record["effective_at"],
                    )
                )
        if not sources:
            return ContextOutcome(status="empty")
        return ContextOutcome(
            status="content",
            sources=tuple(sources),
            character_count=sum(len(source.body) for source in sources),
        )


class BackgroundBudget(Protocol):
    """Attempt budget ledger port; production uses `ProposalAttemptContext`."""

    async def register_call(
        self,
        active,
        node: str,
        kind: Literal["model", "mcp"],
        sources: tuple[UUID, ...],
        parameter_digest: str,
    ): ...


class OpportunityContext:
    """Applies the locked background necessity policy under the Attempt budget ledger."""

    def __init__(
        self,
        reader: ContextReader,
        *,
        limits: ContextLimits | None = None,
        max_attempts: int = 1 + READ_CONTEXT_RETRIES,
        budget: BackgroundBudget | None = None,
        active=None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_attempts < 1 or max_attempts > 1 + READ_CONTEXT_RETRIES:
            raise ValueError("Background attempts must stay within the technical retry cap")
        self.reader, self.clock = reader, clock
        self.limits = limits or ContextLimits()
        self.max_attempts = max_attempts
        self.budget, self.active = budget, active

    async def load(
        self,
        actor: Actor,
        opportunity_id: UUID,
        requirement: BackgroundRequirement,
        *,
        seconds: float | None = None,
        sources: tuple[UUID, ...] = (),
    ) -> ContextOutcome:
        if requirement == BackgroundRequirement.UNCONFIRMED:
            raise NotReady()
        limit = min(
            seconds if seconds is not None else self.limits.read_seconds, self.limits.read_seconds
        )
        attempts = 0
        last: ContextOutcome | None = None
        while attempts < self.max_attempts:
            attempts += 1
            await self._charge_budget(opportunity_id, limit, sources, attempts)
            outcome = await self.reader.read(actor, opportunity_id, seconds=limit)
            if outcome.status in {"content", "empty"}:
                return outcome
            last = outcome
            if outcome.status in {
                "timeout",
                "contract_failed",
                "not_found_or_not_allowed",
                "capacity_exceeded",
            }:
                break
        assert last is not None
        return self._apply_requirement(last, requirement, attempts)

    async def _charge_budget(
        self,
        opportunity_id: UUID,
        seconds: float,
        sources: tuple[UUID, ...],
        ordinal: int,
    ) -> None:
        if self.budget is None:
            return
        await self.budget.register_call(
            self.active,
            f"opportunity-background-{opportunity_id}",
            "mcp",
            sources,
            digest({"opportunity": str(opportunity_id), "seconds": seconds, "ordinal": ordinal}),
        )

    def _apply_requirement(
        self, outcome: ContextOutcome, requirement: BackgroundRequirement, attempts: int
    ) -> ContextOutcome:
        degradation = (f"background_{outcome.status}", f"attempts_{attempts}")
        if requirement == BackgroundRequirement.REQUIRED:
            if outcome.status == "not_found_or_not_allowed":
                raise AccessDenied()
            if outcome.status == "capacity_exceeded":
                raise ContextCapacityExceeded()
            raise ContextUnavailable()
        return ContextOutcome(
            status=outcome.status,
            error_code=outcome.error_code,
            degradation=degradation,
        )


__all__ = [
    "BackgroundSource",
    "BackgroundBudget",
    "ContextContractViolation",
    "ContextCapacityExceeded",
    "ContextLimits",
    "ContextOutcome",
    "ContextReader",
    "ContextUnavailable",
    "MAX_CONTEXT_CHARACTERS",
    "MAX_CONTEXT_RECORDS",
    "OpportunityContext",
    "OpportunityContextReader",
    "READ_CONTEXT_RETRIES",
    "READ_CONTEXT_SECONDS",
]
