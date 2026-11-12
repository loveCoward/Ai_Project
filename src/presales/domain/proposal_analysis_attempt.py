"""Proposal Analysis Attempt contracts shared by commands, storage, and checkpointing."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from presales.domain.contracts import Conflict, DomainError, digest


class AttemptStage(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    RECONCILING = "reconciling"
    FINISHED = "finished"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MATERIALS_REQUIRED = "materials_required"
    CANCELLED = "cancelled"
    AUTHORIZATION_INVALID = "authorization_invalid"


class ModelMode(StrEnum):
    FAKE = "fake"
    REAL = "real"


class BackgroundRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    UNCONFIRMED = "unconfirmed"


class AttemptCommandError(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND_OR_NOT_ALLOWED = "not_found_or_not_allowed"
    REVISION_OR_REQUEST_CONFLICT = "revision_or_request_conflict"
    NOT_READY = "not_ready"
    STORAGE_UNAVAILABLE_RECONCILE_REQUEST = "storage_unavailable_reconcile_request"
    ATTEMPT_TERMINAL = "attempt_terminal"
    FENCE_CONFLICT = "fence_conflict"
    UNSUPPORTED_SAVER = "unsupported_saver"
    GLOBAL_BUSY = "global_busy"
    ATTEMPT_BUSY = "attempt_busy"
    CANCEL_NOT_ALLOWED = "cancel_not_allowed"


class AttemptTerminal(DomainError):
    code = AttemptCommandError.ATTEMPT_TERMINAL.value


class FenceConflict(DomainError):
    code = AttemptCommandError.FENCE_CONFLICT.value


class UnsupportedSaver(DomainError):
    code = AttemptCommandError.UNSUPPORTED_SAVER.value


class GlobalBusy(DomainError):
    code = AttemptCommandError.GLOBAL_BUSY.value


class AttemptBusy(DomainError):
    code = AttemptCommandError.ATTEMPT_BUSY.value


class CancelNotAllowed(DomainError):
    code = AttemptCommandError.CANCEL_NOT_ALLOWED.value


class AttemptBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_active_seconds: int = Field(default=600, ge=1)
    max_model_seconds: int = Field(default=120, ge=1)
    max_mcp_seconds: int = Field(default=10, ge=1)
    max_mcp_retries: int = Field(default=2, ge=0, le=2)
    max_reflections: int = Field(default=1, ge=0, le=1)


class AttemptIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    task_id: UUID
    actor_id: UUID
    thread_id: str = Field(min_length=1, max_length=200)
    input_revision: int = Field(ge=1)
    execution_generation: int = Field(ge=1)


class ProposalAnalysisAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    task_id: UUID
    actor_id: UUID
    thread_id: str = Field(min_length=1, max_length=200)
    input_revision: int = Field(ge=1)
    start_scope: tuple[UUID, ...] = Field(min_length=1, max_length=1000)
    model_mode: ModelMode
    config_identity: str = Field(min_length=1, max_length=200)
    execution_generation: int = Field(default=1, ge=1)
    stage: AttemptStage = AttemptStage.ACCEPTED
    outcome: AttemptOutcome | None = None
    run_revision: int = Field(default=0, ge=0)
    budget: AttemptBudget = Field(default_factory=AttemptBudget)
    reflection_revision: int = Field(default=0, ge=0, le=1)
    cancelled_at: datetime | None = None
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def validate_terminal_consistency(self) -> "ProposalAnalysisAttempt":
        if self.stage == AttemptStage.FINISHED:
            if self.outcome is None or self.terminal_at is None:
                raise ValueError("finished attempts require an outcome and terminal timestamp")
        elif self.outcome is not None or self.terminal_at is not None:
            raise ValueError("non-terminal attempts cannot have an outcome or terminal timestamp")
        return self

    @property
    def identity(self) -> AttemptIdentity:
        return AttemptIdentity(
            attempt_id=self.id,
            task_id=self.task_id,
            actor_id=self.actor_id,
            thread_id=self.thread_id,
            input_revision=self.input_revision,
            execution_generation=self.execution_generation,
        )


class StartAttemptCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    task_id: UUID
    expected_task_revision: int = Field(ge=1)
    expected_input_revision: int = Field(ge=1)
    model_mode: ModelMode
    config_identity: str = Field(min_length=1, max_length=200)
    start_scope: tuple[UUID, ...] = Field(min_length=1, max_length=1000)
    background_requirement: BackgroundRequirement

    @model_validator(mode="after")
    def require_confirmed_background_policy(self) -> "StartAttemptCommand":
        if self.background_requirement == BackgroundRequirement.UNCONFIRMED:
            raise ValueError("background requirement must be explicitly required or optional")
        return self


class CancelAttemptCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    task_id: UUID
    expected_task_revision: int = Field(ge=1)
    attempt_id: UUID | None = None


class RecoveryIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    attempt_id: UUID
    expected_execution_generation: int = Field(ge=1)
    expected_run_revision: int = Field(ge=0)
    expected_task_revision: int = Field(ge=1)
    expected_input_revision: int = Field(ge=1)
    checkpoint_id: str = Field(min_length=1, max_length=200)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttemptCommandReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    operation: str = Field(min_length=1, max_length=100)
    parameter_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: UUID
    result: dict[str, Any]


class RunProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    run_revision: int = Field(ge=0)
    stage: AttemptStage
    outcome: AttemptOutcome | None = None
    node: str | None = Field(default=None, max_length=200)
    error_code: str | None = Field(default=None, max_length=100)
    degradation: tuple[str, ...] = ()
    result_ready: bool = False
    result_revision_id: UUID | None = None
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def validate_result_and_terminal(self) -> "RunProjection":
        if self.stage == AttemptStage.FINISHED:
            if self.outcome is None or self.terminal_at is None:
                raise ValueError("finished projections require an outcome and terminal timestamp")
        elif self.outcome is not None or self.terminal_at is not None:
            raise ValueError("non-terminal projections cannot have an outcome")
        if self.result_ready and self.result_revision_id is None:
            raise ValueError("ready results require a result revision")
        return self


_TRANSITIONS: dict[AttemptStage, frozenset[AttemptStage]] = {
    AttemptStage.ACCEPTED: frozenset(
        {AttemptStage.RUNNING, AttemptStage.RECONCILING, AttemptStage.FINISHED}
    ),
    AttemptStage.RUNNING: frozenset(
        {
            AttemptStage.WAITING_HUMAN,
            AttemptStage.RECONCILING,
            AttemptStage.FINISHED,
        }
    ),
    AttemptStage.WAITING_HUMAN: frozenset(
        {AttemptStage.RUNNING, AttemptStage.RECONCILING, AttemptStage.FINISHED}
    ),
    AttemptStage.RECONCILING: frozenset({AttemptStage.RUNNING, AttemptStage.FINISHED}),
    AttemptStage.FINISHED: frozenset(),
}


def transition_attempt(
    attempt: ProposalAnalysisAttempt,
    target: AttemptStage,
    outcome: AttemptOutcome | None = None,
    *,
    resumed_generation: int | None = None,
) -> ProposalAnalysisAttempt:
    if attempt.cancelled_at is not None and target in {
        AttemptStage.RUNNING,
        AttemptStage.WAITING_HUMAN,
    }:
        raise AttemptTerminal()
    if attempt.stage == AttemptStage.RECONCILING and target == AttemptStage.RUNNING:
        if resumed_generation != attempt.execution_generation + 1:
            raise Conflict()
    if target not in _TRANSITIONS[attempt.stage]:
        if attempt.stage == AttemptStage.FINISHED:
            raise AttemptTerminal()
        raise Conflict()
    if target == AttemptStage.FINISHED:
        if outcome is None:
            raise ValueError("finished attempts require an outcome")
        terminal_at = datetime.now(UTC)
        return attempt.model_copy(
            update={
                "stage": target,
                "outcome": outcome,
                "run_revision": attempt.run_revision + 1,
                "terminal_at": terminal_at,
                "cancelled_at": (
                    attempt.cancelled_at or terminal_at
                    if outcome in TERMINAL_OUTCOMES
                    else attempt.cancelled_at
                ),
            }
        )
    if outcome is not None:
        raise ValueError("non-terminal attempts cannot have an outcome")
    update: dict[str, Any] = {
        "stage": target,
        "run_revision": attempt.run_revision + 1,
    }
    if target == AttemptStage.RUNNING and resumed_generation is not None:
        update["execution_generation"] = resumed_generation
    return attempt.model_copy(update=update)


def command_fingerprint(operation: str, request_id: UUID, parameters: dict[str, Any]) -> str:
    return digest(
        {
            "operation": operation,
            "request_id": str(request_id),
            "parameters": parameters,
        }
    )


def validate_command_receipt(receipt: AttemptCommandReceipt, parameters: dict[str, Any]) -> None:
    expected = command_fingerprint(receipt.operation, receipt.request_id, parameters)
    if receipt.parameter_digest != expected:
        raise Conflict()


def command_error_code(error: DomainError) -> AttemptCommandError:
    try:
        return AttemptCommandError(error.code)
    except ValueError:
        return AttemptCommandError.STORAGE_UNAVAILABLE_RECONCILE_REQUEST


def next_projection(
    projection: RunProjection,
    *,
    stage: AttemptStage | None = None,
    outcome: AttemptOutcome | None = None,
    node: str | None = None,
    error_code: str | None = None,
    degradation: tuple[str, ...] | None = None,
    result_ready: bool | None = None,
    result_revision_id: UUID | None = None,
    terminal_at: datetime | None = None,
) -> RunProjection:
    target_stage = stage or projection.stage
    if projection.stage == AttemptStage.FINISHED:
        raise AttemptTerminal()
    if target_stage == AttemptStage.FINISHED and outcome is None:
        raise ValueError("finished projections require an outcome")
    return RunProjection(
        attempt_id=projection.attempt_id,
        run_revision=projection.run_revision + 1,
        stage=target_stage,
        outcome=outcome if target_stage == AttemptStage.FINISHED else None,
        node=node if node is not None else projection.node,
        error_code=error_code if error_code is not None else projection.error_code,
        degradation=degradation if degradation is not None else projection.degradation,
        result_ready=result_ready if result_ready is not None else projection.result_ready,
        result_revision_id=(
            result_revision_id if result_revision_id is not None else projection.result_revision_id
        ),
        terminal_at=(
            terminal_at
            if terminal_at is not None
            else (datetime.now(UTC) if target_stage == AttemptStage.FINISHED else None)
        ),
    )


TERMINAL_OUTCOMES = frozenset(
    {
        AttemptOutcome.CANCELLED,
        AttemptOutcome.AUTHORIZATION_INVALID,
    }
)

__all__ = [
    "AttemptBudget",
    "AttemptBusy",
    "AttemptCommandError",
    "AttemptCommandReceipt",
    "AttemptIdentity",
    "AttemptOutcome",
    "AttemptStage",
    "AttemptTerminal",
    "BackgroundRequirement",
    "CancelAttemptCommand",
    "CancelNotAllowed",
    "FenceConflict",
    "GlobalBusy",
    "ModelMode",
    "ProposalAnalysisAttempt",
    "RecoveryIntent",
    "RunProjection",
    "StartAttemptCommand",
    "TERMINAL_OUTCOMES",
    "UnsupportedSaver",
    "command_error_code",
    "command_fingerprint",
    "next_projection",
    "transition_attempt",
    "validate_command_receipt",
]
