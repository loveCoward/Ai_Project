from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from presales.domain.contracts import AccessDenied, Conflict, TaskStatus
from presales.domain.proposal_analysis_attempt import (
    AttemptBudget,
    AttemptCommandError,
    AttemptCommandReceipt,
    AttemptOutcome,
    AttemptStage,
    AttemptTerminal,
    BackgroundRequirement,
    FenceConflict,
    GlobalBusy,
    ModelMode,
    ProposalAnalysisAttempt,
    RunProjection,
    StartAttemptCommand,
    command_error_code,
    command_fingerprint,
    next_projection,
    transition_attempt,
    validate_command_receipt,
)


def attempt() -> ProposalAnalysisAttempt:
    return ProposalAnalysisAttempt(
        id=uuid4(),
        task_id=uuid4(),
        actor_id=uuid4(),
        thread_id="attempt-thread",
        input_revision=1,
        start_scope=(uuid4(),),
        model_mode=ModelMode.FAKE,
        config_identity="fake-config",
    )


def test_attempt_stage_and_outcome_contract():
    value = attempt()
    running = transition_attempt(value, AttemptStage.RUNNING)
    finished = transition_attempt(running, AttemptStage.FINISHED, AttemptOutcome.SUCCEEDED)
    assert running.run_revision == 1
    assert finished.run_revision == 2
    assert finished.outcome == AttemptOutcome.SUCCEEDED
    assert finished.terminal_at is not None
    assert TaskStatus.ANALYZING.value == "analyzing"
    assert "running" not in {item.value for item in TaskStatus}


def test_forged_actor_thread_and_endpoint_are_rejected():
    base = {
        "request_id": uuid4(),
        "task_id": uuid4(),
        "expected_task_revision": 1,
        "expected_input_revision": 1,
        "model_mode": "fake",
        "config_identity": "fake-config",
        "start_scope": [uuid4()],
        "background_requirement": "required",
    }
    for field in ("actor", "thread_id", "endpoint"):
        payload = dict(base)
        payload[field] = "forged"
        with pytest.raises(ValidationError):
            StartAttemptCommand.model_validate(payload)
    payload = dict(base)
    payload["background_requirement"] = BackgroundRequirement.UNCONFIRMED.value
    with pytest.raises(ValidationError):
        StartAttemptCommand.model_validate(payload)


def test_receipt_replay_is_stable_and_conflicts_on_new_arguments():
    request_id = uuid4()
    parameters = {"task": str(uuid4()), "mode": "fake"}
    receipt = AttemptCommandReceipt(
        request_id=request_id,
        operation="start_attempt",
        parameter_digest=command_fingerprint("start_attempt", request_id, parameters),
        attempt_id=uuid4(),
        result={"accepted": True},
    )
    validate_command_receipt(receipt, parameters)
    with pytest.raises(Conflict):
        validate_command_receipt(receipt, {"task": str(uuid4()), "mode": "fake"})


def test_terminal_attempts_cannot_revive_and_outcome_is_terminal():
    value = attempt()
    finished = transition_attempt(value, AttemptStage.FINISHED, AttemptOutcome.CANCELLED)
    with pytest.raises(AttemptTerminal):
        transition_attempt(finished, AttemptStage.RUNNING)
    authorized = transition_attempt(
        value, AttemptStage.FINISHED, AttemptOutcome.AUTHORIZATION_INVALID
    )
    with pytest.raises(AttemptTerminal):
        transition_attempt(authorized, AttemptStage.RUNNING)
    with pytest.raises(ValueError):
        transition_attempt(value, AttemptStage.FINISHED)


def test_projection_revision_is_monotonic_and_terminal_is_not_recoverable():
    projection = RunProjection(
        attempt_id=uuid4(),
        run_revision=0,
        stage=AttemptStage.ACCEPTED,
    )
    running = next_projection(projection, stage=AttemptStage.RUNNING, node="requirements")
    finished = next_projection(
        running, stage=AttemptStage.FINISHED, outcome=AttemptOutcome.SUCCEEDED
    )
    assert running.run_revision == 1
    assert finished.run_revision == 2
    with pytest.raises(AttemptTerminal):
        next_projection(finished, stage=AttemptStage.RUNNING)


def test_command_errors_are_decidable_and_budget_bounds_are_explicit():
    assert command_error_code(AttemptTerminal()) == AttemptCommandError.ATTEMPT_TERMINAL
    assert command_error_code(FenceConflict()) == AttemptCommandError.FENCE_CONFLICT
    assert command_error_code(GlobalBusy()) == AttemptCommandError.GLOBAL_BUSY
    assert command_error_code(AccessDenied()) == AttemptCommandError.NOT_FOUND_OR_NOT_ALLOWED
    with pytest.raises(ValidationError):
        AttemptBudget(max_reflections=2)


def test_projection_rejects_finished_without_outcome():
    with pytest.raises(ValidationError):
        RunProjection(
            attempt_id=uuid4(),
            run_revision=1,
            stage=AttemptStage.FINISHED,
            terminal_at=datetime.now(UTC),
        )


def test_cancelled_attempt_cannot_run_again_before_execution_drains():
    cancelled = attempt().model_copy(update={"cancelled_at": datetime.now(UTC)})
    with pytest.raises(AttemptTerminal):
        transition_attempt(cancelled, AttemptStage.RUNNING)


def test_terminal_projection_outcome_cannot_be_replaced():
    projection = RunProjection(
        attempt_id=uuid4(),
        run_revision=2,
        stage=AttemptStage.FINISHED,
        outcome=AttemptOutcome.CANCELLED,
        terminal_at=datetime.now(UTC),
    )
    with pytest.raises(AttemptTerminal):
        next_projection(projection, stage=AttemptStage.FINISHED, outcome=AttemptOutcome.SUCCEEDED)
