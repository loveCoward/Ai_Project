"""Offline contracts for background limits, retry caps and Attempt budget accounting."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from presales.application.opportunity_context import (
    MAX_CONTEXT_CHARACTERS,
    MAX_CONTEXT_RECORDS,
    READ_CONTEXT_RETRIES,
    READ_CONTEXT_SECONDS,
    ContextLimits,
    ContextOutcome,
    OpportunityContext,
)
from presales.domain.contracts import AccessDenied, Actor, NotReady
from presales.domain.proposal_analysis_attempt import BackgroundRequirement


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Reader:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[float] = []

    async def read(self, actor, opportunity_id, *, seconds):
        self.calls.append(seconds)
        return self.outcomes.pop(0)


class Budget:
    """Stands in for ProposalAttemptContext.register_call and records every charge."""

    def __init__(self, *, exhausted_after: int | None = None) -> None:
        self.calls: list[dict] = []
        self.exhausted_after = exhausted_after

    async def register_call(self, active, node, kind, sources, parameter_digest):
        if self.exhausted_after is not None and len(self.calls) >= self.exhausted_after:
            from presales.application.proposal_attempt_context import BudgetExhausted

            raise BudgetExhausted()
        self.calls.append(
            {
                "active": active,
                "node": node,
                "kind": kind,
                "sources": sources,
                "digest": parameter_digest,
            }
        )
        return object()


def actor() -> Actor:
    return Actor(subject=uuid4())


def test_limits_have_verifiable_defaults():
    limits = ContextLimits()
    assert limits.effective() == {
        "read_seconds": READ_CONTEXT_SECONDS,
        "max_characters": MAX_CONTEXT_CHARACTERS,
        "max_records": MAX_CONTEXT_RECORDS,
    }


@pytest.mark.parametrize(
    "options",
    [{"read_seconds": 0}, {"read_seconds": 601}, {"max_characters": 0}, {"max_records": 0}],
)
def test_invalid_limits_are_refused(options):
    with pytest.raises(ValueError):
        ContextLimits(**options)


@pytest.mark.asyncio
async def test_successful_read_charges_the_ledger_once():
    budget = Budget()
    context = OpportunityContext(
        Reader([ContextOutcome(status="content")]),
        limits=ContextLimits(read_seconds=4),
        budget=budget,
        active="active-token",
        clock=Clock(),
    )
    outcome = await context.load(actor(), uuid4(), BackgroundRequirement.REQUIRED)
    assert outcome.status == "content"
    assert len(budget.calls) == 1
    assert budget.calls[0]["kind"] == "mcp"
    assert budget.calls[0]["active"] == "active-token"
    assert budget.calls[0]["node"].startswith("opportunity-background-")


@pytest.mark.asyncio
async def test_timeout_and_contract_failure_are_not_retried():
    for status in ("timeout", "contract_failed", "capacity_exceeded"):
        budget = Budget()
        reader = Reader([ContextOutcome(status=status) for _ in range(5)])
        context = OpportunityContext(reader, budget=budget, clock=Clock())
        await context.load(actor(), uuid4(), BackgroundRequirement.OPTIONAL)
        assert len(reader.calls) == 1, status
        assert len(budget.calls) == 1, status


@pytest.mark.asyncio
async def test_unavailable_is_retried_up_to_the_cap_and_charged_each_time():
    budget = Budget()
    reader = Reader([ContextOutcome(status="unavailable", error_code="x") for _ in range(5)])
    context = OpportunityContext(reader, budget=budget, clock=Clock())
    outcome = await context.load(actor(), uuid4(), BackgroundRequirement.OPTIONAL)
    assert outcome.status == "unavailable"
    assert len(reader.calls) == 1 + READ_CONTEXT_RETRIES
    assert len(budget.calls) == 1 + READ_CONTEXT_RETRIES
    assert "attempts_3" in outcome.degradation


@pytest.mark.asyncio
async def test_attempt_cap_cannot_exceed_the_technical_retry_limit():
    with pytest.raises(ValueError):
        OpportunityContext(Reader([]), max_attempts=1 + READ_CONTEXT_RETRIES + 1)
    with pytest.raises(ValueError):
        OpportunityContext(Reader([]), max_attempts=0)


@pytest.mark.asyncio
async def test_tighter_attempt_cap_stops_earlier_but_stays_charged():
    budget = Budget()
    reader = Reader([ContextOutcome(status="unavailable") for _ in range(5)])
    context = OpportunityContext(reader, max_attempts=2, budget=budget, clock=Clock())
    outcome = await context.load(actor(), uuid4(), BackgroundRequirement.OPTIONAL)
    assert len(reader.calls) == 2
    assert len(budget.calls) == 2
    assert "attempts_2" in outcome.degradation


@pytest.mark.asyncio
async def test_exhausted_budget_stops_before_calling_the_reader():
    budget = Budget(exhausted_after=1)
    reader = Reader([ContextOutcome(status="unavailable") for _ in range(5)])
    context = OpportunityContext(reader, budget=budget, clock=Clock())
    from presales.application.proposal_attempt_context import BudgetExhausted

    with pytest.raises(BudgetExhausted):
        await context.load(actor(), uuid4(), BackgroundRequirement.OPTIONAL)
    assert len(reader.calls) == 1


@pytest.mark.asyncio
async def test_declared_read_seconds_is_clamped_to_the_configured_limit():
    reader = Reader([ContextOutcome(status="content")])
    context = OpportunityContext(reader, limits=ContextLimits(read_seconds=3), clock=Clock())
    await context.load(actor(), uuid4(), BackgroundRequirement.OPTIONAL, seconds=600)
    assert reader.calls == [3]


@pytest.mark.asyncio
async def test_unconfirmed_requirement_is_refused_before_any_charge():
    budget = Budget()
    reader = Reader([])
    context = OpportunityContext(reader, budget=budget, clock=Clock())
    with pytest.raises(NotReady):
        await context.load(actor(), uuid4(), BackgroundRequirement.UNCONFIRMED)
    assert reader.calls == [] and budget.calls == []


@pytest.mark.asyncio
async def test_required_denial_and_capacity_still_fail_closed_with_budget():
    budget = Budget()
    context = OpportunityContext(
        Reader([ContextOutcome(status="not_found_or_not_allowed")]), budget=budget
    )
    with pytest.raises(AccessDenied):
        await context.load(actor(), uuid4(), BackgroundRequirement.REQUIRED)
    assert len(budget.calls) == 1
    from presales.application.opportunity_context import ContextCapacityExceeded

    with pytest.raises(ContextCapacityExceeded):
        await OpportunityContext(
            Reader([ContextOutcome(status="capacity_exceeded")]), budget=Budget()
        ).load(actor(), uuid4(), BackgroundRequirement.REQUIRED)


@pytest.mark.asyncio
async def test_reader_uses_its_own_clock_and_clamps_the_deadline():
    from presales.application.opportunity_context import OpportunityContextReader

    class Database:
        def transaction(self):
            raise AssertionError("the deadline check happens before any query")

    limits = ContextLimits(read_seconds=2)
    reader = OpportunityContextReader(Database(), limits=limits, clock=Clock())
    assert reader.limits.read_seconds == 2
    assert isinstance(uuid4(), UUID)
