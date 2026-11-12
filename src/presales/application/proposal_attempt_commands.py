"""Atomic start and cancellation commands for Proposal Analysis Attempts."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from presales.access.policy import Policy
from presales.application.opportunity_context import ContextLimits
from presales.application.service import audit
from presales.domain.contracts import (
    AccessDenied,
    Actor,
    Conflict,
    InvalidInput,
    NotReady,
    digest,
)
from presales.domain.proposal_analysis_attempt import (
    AttemptBusy,
    AttemptStage,
    AttemptTerminal,
    BackgroundRequirement,
    CancelAttemptCommand,
    CancelNotAllowed,
    FenceConflict,
    ProposalAnalysisAttempt,
    StartAttemptCommand,
)
from presales.infrastructure.database import AUTH_LOCK, Database, lock
from presales.infrastructure.proposal_attempt_store import ProposalAttemptStore
from presales.infrastructure.proposal_execution_records import (
    ProposalExecutionRecord,
    ProposalExecutionRecords,
)
from presales.infrastructure.proposal_model import ProposalModel
from presales.materials.models import PARSER_VERSION

START_OPERATION = "start_proposal_attempt"
CANCEL_OPERATION = "cancel_proposal_task"
STARTABLE_TASK_STATES = frozenset(
    {"new", "awaiting_supplementation", "failed", "analyzed_awaiting_submission"}
)
CANCELLABLE_TASK_STATES = frozenset(
    {
        "new",
        "analyzing",
        "analyzed_awaiting_submission",
        "awaiting_supplementation",
        "failed",
        "awaiting_review",
    }
)
BACKGROUND_POLICY_KEY = "background-policy"
BACKGROUND_LIMITS_KEY = "background-limits"


class AttemptStartSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_key_input_characters: int = Field(default=60_000, ge=1, le=1_000_000)
    slot_lease_seconds: int = Field(default=30, ge=5, le=600)
    background_limits: ContextLimits = Field(default_factory=ContextLimits)


def start_parameters(command: StartAttemptCommand) -> dict:
    return command.model_dump(mode="json", exclude={"request_id"})


def cancel_parameters(command: CancelAttemptCommand) -> dict:
    return command.model_dump(mode="json", exclude={"request_id"})


class ProposalAttemptStart:
    """One legal command atomically creates the attempt, slots, state and receipt."""

    def __init__(
        self,
        database: Database,
        *,
        model: ProposalModel | None = None,
        settings: AttemptStartSettings | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database, self.model, self.clock = database, model, clock
        self.settings = settings or AttemptStartSettings()

    async def start(self, actor: Actor, command: StartAttemptCommand) -> dict:
        fingerprint = digest({"target": str(command.task_id), **start_parameters(command)})
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await policy.resource(command.task_id)
            store = ProposalAttemptStore(conn)
            await lock(conn, AUTH_LOCK, shared=True)
            await store.global_slot_for_start()
            task = await store.task_for_start(command.task_id)
            if task is None:
                raise AccessDenied()
            replay = await store.receipt(actor.subject, START_OPERATION, command.request_id)
            if replay is not None:
                if replay["parameter_digest"] != fingerprint:
                    raise Conflict()
                return dict(replay["result"])
            if await store.active_attempt(command.task_id) is not None:
                raise AttemptBusy()
            self._require_startable(task, command)
            inspected = await self._inspect_inputs(store, command, policy)
            required, missing_optional = self._classify(inspected)
            background_characters = await store.background_characters(task["opportunity_id"])
            self._require_capacity(required, background_characters)
            allowed = await self._declared_scope(command, policy)
            self._require_scope(inspected, allowed)
            declarations = await store.declarations(tuple(item["version_id"] for item in inspected))
            self._require_model(command, declarations)
            attempt = self._attempt(actor, command)
            await store.accept_attempt(
                attempt,
                actor=actor.subject,
                owner=actor.subject,
                lease_until=self.clock() + timedelta(seconds=self.settings.slot_lease_seconds),
            )
            await ProposalExecutionRecords(conn).save(
                _policy_record(attempt, command.background_requirement)
            )
            background_limits = self.settings.background_limits.effective()
            await ProposalExecutionRecords(conn).save(_limits_record(attempt, background_limits))
            result = {
                "attempt_id": str(attempt.id),
                "task_id": str(attempt.task_id),
                "thread_id": attempt.thread_id,
                "input_revision": attempt.input_revision,
                "execution_generation": attempt.execution_generation,
                "run_revision": 1,
                "stage": AttemptStage.ACCEPTED.value,
                "model_mode": attempt.model_mode.value,
                "config_identity": attempt.config_identity,
                "background_requirement": command.background_requirement.value,
                "background_limits": background_limits,
                "start_scope": sorted(str(item) for item in attempt.start_scope),
                "missing_optional_sources": sorted(
                    str(item["version_id"]) for item in missing_optional
                ),
                "resource_id": str(attempt.task_id),
            }
            await store.write_receipt(
                actor.subject, START_OPERATION, command.request_id, fingerprint, result
            )
            await audit(
                conn,
                str(actor.subject),
                START_OPERATION,
                command.task_id,
                "accepted",
                policy.authorization_revision,
                command.request_id,
                task["revision"],
            )
            return result

    def _require_startable(self, task: dict, command: StartAttemptCommand) -> None:
        if task["revision"] != command.expected_task_revision or (
            task["input_revision"] != command.expected_input_revision
        ):
            raise Conflict()
        if task["state"] == "cancelled":
            raise AttemptTerminal()
        if task["state"] not in STARTABLE_TASK_STATES:
            raise Conflict()

    async def _inspect_inputs(
        self, store: ProposalAttemptStore, command: StartAttemptCommand, policy: Policy
    ) -> list[dict]:
        necessity = await store.input_necessity(command.task_id, command.expected_input_revision)
        inspected = []
        for version_id in await store.task_inputs(command.task_id):
            requirement = necessity.get(version_id, "unclassified")
            try:
                await policy.source_closure(version_id)
                accessible = True
            except AccessDenied:
                accessible = False
            parsed = await store.parsed_size(version_id, PARSER_VERSION)
            inspected.append(
                {
                    "version_id": version_id,
                    "requirement": requirement,
                    "accessible": accessible,
                    "parsed": parsed is not None and parsed["state"] == "succeeded",
                    "characters": (parsed or {}).get("character_count") or 0,
                }
            )
        return inspected

    def _classify(self, inspected: list[dict]) -> tuple[list[dict], list[dict]]:
        required = [item for item in inspected if item["requirement"] == "required"]
        optional = [item for item in inspected if item["requirement"] == "optional"]
        if not required or any(
            item["requirement"] not in {"required", "optional"} for item in inspected
        ):
            raise NotReady()
        if any(not item["accessible"] or not item["parsed"] for item in required):
            raise NotReady()
        return required, [item for item in optional if not item["accessible"] or not item["parsed"]]

    def _require_capacity(self, required: list[dict], background_characters: int = 0) -> None:
        characters = sum(item["characters"] for item in required) + background_characters
        if characters > self.settings.max_key_input_characters:
            raise InvalidInput()

    async def _declared_scope(self, command: StartAttemptCommand, policy: Policy) -> set[UUID]:
        resolved = set(command.start_scope)
        for source in command.start_scope:
            try:
                resolved.update(await policy.source_closure(source))
            except AccessDenied:
                continue
        return resolved

    def _require_scope(self, inputs: list[dict], allowed: set[UUID]) -> None:
        if any(
            item["version_id"] not in allowed
            for item in inputs
            if item["requirement"] == "required"
        ):
            raise AccessDenied()

    def _require_model(self, command: StartAttemptCommand, declarations: set[str]) -> None:
        if self.model is None:
            raise InvalidInput()
        self.model.preflight(
            {"config_identity": command.config_identity, "model_mode": command.model_mode.value},
            declarations,
        )

    def _attempt(self, actor: Actor, command: StartAttemptCommand) -> ProposalAnalysisAttempt:
        attempt_id = uuid4()
        return ProposalAnalysisAttempt(
            id=attempt_id,
            task_id=command.task_id,
            actor_id=actor.subject,
            thread_id=f"attempt-{attempt_id}",
            input_revision=command.expected_input_revision,
            start_scope=command.start_scope,
            model_mode=command.model_mode,
            config_identity=command.config_identity,
            execution_generation=1,
            stage=AttemptStage.ACCEPTED,
            run_revision=0,
        )


class ProposalAttemptCancel:
    """Cancels an unapproved task and fences its active Attempt before any drain."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def cancel(self, actor: Actor, command: CancelAttemptCommand) -> dict:
        fingerprint = digest({"target": str(command.task_id), **cancel_parameters(command)})
        async with self.database.transaction() as conn:
            policy = Policy(conn, actor)
            await policy.start(architect=True)
            await policy.resource(command.task_id)
            store = ProposalAttemptStore(conn)
            await lock(conn, AUTH_LOCK, shared=True)
            replay = await store.receipt(actor.subject, CANCEL_OPERATION, command.request_id)
            if replay is not None:
                if replay["parameter_digest"] != fingerprint:
                    raise Conflict()
                return dict(replay["result"])
            task = await store.task_for_cancel(command.task_id)
            if task is None:
                raise AccessDenied()
            if task["state"] == "cancelled":
                if command.attempt_id is None:
                    raise InvalidInput()
                attempt = await store.attempt_for_cancel(command.attempt_id)
                if attempt is None or attempt["task_id"] != command.task_id:
                    raise Conflict()
                result = self._result(task, attempt)
                await store.write_receipt(
                    actor.subject,
                    CANCEL_OPERATION,
                    command.request_id,
                    fingerprint,
                    result,
                )
                return result
            self._require_cancellable(task, command)
            if command.attempt_id is None:
                raise InvalidInput()
            attempt = await store.attempt_for_cancel(command.attempt_id)
            if attempt is None or attempt["task_id"] != command.task_id:
                raise Conflict()
            if attempt["execution_stage"] == AttemptStage.FINISHED.value:
                if attempt["outcome"] == "succeeded":
                    task_revision = await store.cancel_task(
                        actor.subject, command.task_id, command.expected_task_revision
                    )
                    result = self._result(
                        {"id": command.task_id, "state": "cancelled", "revision": task_revision},
                        attempt,
                    )
                    await store.write_receipt(
                        actor.subject,
                        CANCEL_OPERATION,
                        command.request_id,
                        fingerprint,
                        result,
                    )
                    return result
                raise AttemptTerminal()
            if attempt["cancelled_at"] is not None:
                task_revision = task["revision"]
                if task["state"] != "cancelled":
                    task_revision = await store.cancel_task(
                        actor.subject, command.task_id, command.expected_task_revision
                    )
                result = self._result(
                    {"id": command.task_id, "state": "cancelled", "revision": task_revision},
                    attempt,
                )
                await store.write_receipt(
                    actor.subject,
                    CANCEL_OPERATION,
                    command.request_id,
                    fingerprint,
                    result,
                )
                return result
            for slot in (
                await store.task_slot_for_cancel(command.task_id),
                await store.global_slot_for_cancel(),
            ):
                if (
                    slot is None
                    or slot["attempt_id"] != attempt["id"]
                    or slot["execution_generation"] != attempt["execution_generation"]
                ):
                    raise FenceConflict()
            await store.mark_cancelled(
                attempt["id"],
                expected_generation=attempt["execution_generation"],
                expected_run_revision=attempt["run_revision"],
            )
            task_revision = await store.cancel_task(
                actor.subject, command.task_id, command.expected_task_revision
            )
            cancelled_attempt = await store.attempt_for_cancel(attempt["id"])
            if cancelled_attempt is None:
                raise AccessDenied()
            result = self._result(
                {"id": command.task_id, "state": "cancelled", "revision": task_revision},
                cancelled_attempt,
            )
            await store.write_receipt(
                actor.subject,
                CANCEL_OPERATION,
                command.request_id,
                fingerprint,
                result,
            )
            await audit(
                conn,
                str(actor.subject),
                CANCEL_OPERATION,
                command.task_id,
                "cancelled",
                policy.authorization_revision,
                command.request_id,
                task_revision,
            )
            return result

    def _require_cancellable(self, task: dict, command: CancelAttemptCommand) -> None:
        if task["revision"] != command.expected_task_revision:
            raise Conflict()
        if task["state"] in {"approved", "completed"}:
            raise CancelNotAllowed()
        if task["state"] not in CANCELLABLE_TASK_STATES:
            raise CancelNotAllowed()

    @staticmethod
    def _result(task: dict, attempt: dict) -> dict:
        return {
            "task_id": str(task["id"]),
            "task_state": task["state"],
            "task_revision": task["revision"],
            "attempt_id": str(attempt["id"]),
            "attempt_stage": attempt["execution_stage"],
            "attempt_outcome": attempt["outcome"],
            "run_revision": attempt["run_revision"],
            "execution_generation": attempt["execution_generation"],
            "drain": "reconciling",
        }


def _policy_record(
    attempt: ProposalAnalysisAttempt,
    requirement: BackgroundRequirement,
    limits: dict | None = None,
):
    body: dict = {"requirement": requirement.value, "locked": True}
    if limits is not None:
        body["limits"] = limits
    return ProposalExecutionRecord(
        attempt_id=attempt.id,
        kind="recovery",
        record_key=BACKGROUND_POLICY_KEY,
        execution_generation=attempt.execution_generation,
        input_digest=digest(body),
        status="completed",
        body=body,
    )


def _limits_record(attempt: ProposalAnalysisAttempt, limits: dict):
    body = {"type": "background-limits", "limits": limits}
    return ProposalExecutionRecord(
        attempt_id=attempt.id,
        kind="recovery",
        record_key=BACKGROUND_LIMITS_KEY,
        execution_generation=attempt.execution_generation,
        input_digest=digest(body),
        status="completed",
        body=body,
    )


__all__ = [
    "AttemptStartSettings",
    "BACKGROUND_POLICY_KEY",
    "BACKGROUND_LIMITS_KEY",
    "CANCELLABLE_TASK_STATES",
    "CANCEL_OPERATION",
    "ProposalAttemptCancel",
    "ProposalAttemptStart",
    "START_OPERATION",
    "STARTABLE_TASK_STATES",
    "cancel_parameters",
    "start_parameters",
]
