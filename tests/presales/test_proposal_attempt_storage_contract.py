from pathlib import Path

from presales.infrastructure.proposal_attempt_store import (
    CANCEL_ATTEMPT_SQL,
    CLAIM_GLOBAL_SLOT_SQL,
    CLAIM_TASK_SLOT_SQL,
    COMMIT_RESULT_SQL,
    CREATE_ATTEMPT_SQL,
    RELEASE_TASK_SLOT_SQL,
    TRANSITION_SQL,
    UPDATE_PROJECTION_SQL,
)
from scripts.test_presales_postgres import (
    DEFAULT_PYTEST_TIMEOUT_SECONDS,
    FULL_SUITE_PYTEST_TIMEOUT_SECONDS,
    SELECTED_PYTEST_TIMEOUT_SECONDS,
    select_pytest_timeout,
)


def test_migration_has_attempt_slots_guards_and_minimal_grants():
    migration = Path("migrations/versions/0006_proposal_attempt.py").read_text(encoding="utf-8")
    assert 'revision = "0006_proposal_attempt"' in migration
    assert 'down_revision = "0005_retrieval"' in migration
    assert "CREATE TABLE presales.proposal_analysis_attempts" in migration
    assert "CREATE TABLE presales.proposal_task_slots" in migration
    assert "CREATE TABLE presales.proposal_global_slot" in migration
    assert "one_active_attempt_per_task" in migration
    assert "UNIQUE" in migration and "thread_id" in migration
    assert "guard_proposal_attempt" in migration
    assert "GRANT UPDATE(execution_generation,execution_stage,outcome,run_revision" in migration
    assert "GRANT SELECT,INSERT ON presales.proposal_analysis_attempts" in migration
    assert "TO presales_runtime" in migration
    assert "TO presales_maintainer" in migration
    assert "GRANT ALL" not in migration
    assert "DELETE FROM presales.proposal_analysis_attempts" not in migration


def test_store_writes_are_conditional_on_generation_revision_and_slot():
    assert "execution_generation" in CREATE_ATTEMPT_SQL
    assert "ON CONFLICT (task_id) DO NOTHING" in CLAIM_TASK_SLOT_SQL
    assert "attempt_id IS NULL" in CLAIM_GLOBAL_SLOT_SQL
    assert "execution_generation=:generation" in UPDATE_PROJECTION_SQL
    assert "run_revision=:expected_run_revision" in UPDATE_PROJECTION_SQL
    assert "execution_stage<>'finished'" in UPDATE_PROJECTION_SQL
    assert "execution_stage=:from_stage" in TRANSITION_SQL
    assert "cancelled_at IS NULL" in COMMIT_RESULT_SQL
    assert "result_ready=false" in COMMIT_RESULT_SQL
    assert "execution_generation=:generation" in CANCEL_ATTEMPT_SQL
    assert "execution_generation=:generation" in RELEASE_TASK_SLOT_SQL


def test_postgres_test_timeout_is_tiered_by_usage():
    assert select_pytest_timeout(full_suite=False, pytest_args=[]) == (
        DEFAULT_PYTEST_TIMEOUT_SECONDS
    )
    assert select_pytest_timeout(full_suite=False, pytest_args=["-q", "-k", "attempt"]) == (
        SELECTED_PYTEST_TIMEOUT_SECONDS
    )
    assert select_pytest_timeout(full_suite=True, pytest_args=["-q"]) == (
        FULL_SUITE_PYTEST_TIMEOUT_SECONDS
    )
    assert SELECTED_PYTEST_TIMEOUT_SECONDS > DEFAULT_PYTEST_TIMEOUT_SECONDS
    assert FULL_SUITE_PYTEST_TIMEOUT_SECONDS > SELECTED_PYTEST_TIMEOUT_SECONDS
