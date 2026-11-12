"""Fenced resume of a reconciling Attempt that holds a recorded recovery intent."""

from alembic import op

revision = "0009_proposal_attempt_recovery"
down_revision = "0008_proposal_attempt_cancel"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION presales.guard_proposal_attempt() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_proposal_attempt'; END IF;
      IF OLD.execution_stage='finished' THEN
        RAISE EXCEPTION 'terminal_proposal_attempt_immutable';
      END IF;
      IF NEW.cancelled_at IS NOT NULL AND NEW.execution_stage IN ('running','waiting_human') THEN
        RAISE EXCEPTION 'cancelled_proposal_attempt_cannot_run';
      END IF;
      IF (NEW.execution_generation <> OLD.execution_generation
          AND (NEW.execution_generation <> OLD.execution_generation + 1
            OR OLD.execution_stage='finished'))
        OR (to_jsonb(NEW)-'execution_generation'-'execution_stage'-'outcome'-'run_revision'-'result_ready'
            -'result_revision_id'-'candidate_version_id'-'source_snapshot'-'cancelled_at'
            -'terminal_at'-'error_code'-'updated_at')
          <> (to_jsonb(OLD)-'execution_generation'-'execution_stage'-'outcome'-'run_revision'-'result_ready'
            -'result_revision_id'-'candidate_version_id'-'source_snapshot'-'cancelled_at'
            -'terminal_at'-'error_code'-'updated_at') THEN
        RAISE EXCEPTION 'immutable_proposal_attempt_identity';
      END IF;
      IF NEW.run_revision <> OLD.run_revision + 1 THEN
        RAISE EXCEPTION 'proposal_attempt_revision_must_increment';
      END IF;
      IF OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS NULL THEN
        RAISE EXCEPTION 'proposal_attempt_cancellation_immutable';
      END IF;
      IF NEW.execution_stage <> OLD.execution_stage AND NOT (
        (OLD.execution_stage='accepted' AND NEW.execution_stage IN ('running','reconciling','finished'))
        OR (OLD.execution_stage='running' AND NEW.execution_stage IN ('waiting_human','reconciling','finished'))
        OR (OLD.execution_stage='waiting_human' AND NEW.execution_stage IN ('running','reconciling','finished'))
        OR (OLD.execution_stage='reconciling' AND NEW.execution_stage='finished')
        OR (OLD.execution_stage='reconciling' AND NEW.execution_stage='running'
            AND NEW.execution_generation = OLD.execution_generation + 1
            AND EXISTS (
              SELECT 1 FROM presales.proposal_execution_records r
              WHERE r.attempt_id=NEW.id AND r.kind='recovery'
                AND r.record_key LIKE 'recovery-intent-%'
                AND r.execution_generation=OLD.execution_generation
                AND r.status='completed'
            ))
      ) THEN
        RAISE EXCEPTION 'invalid_proposal_attempt_transition';
      END IF;
      IF OLD.execution_stage='finished' AND NEW.outcome IS DISTINCT FROM OLD.outcome THEN
        RAISE EXCEPTION 'terminal_proposal_attempt_outcome_immutable';
      END IF;
      IF NEW.execution_stage='finished' AND NEW.terminal_at IS NULL THEN
        RAISE EXCEPTION 'terminal_proposal_attempt_requires_timestamp';
      END IF;
      IF NEW.execution_stage<>'finished' AND NEW.terminal_at IS NOT NULL THEN
        RAISE EXCEPTION 'nonterminal_proposal_attempt_cannot_have_terminal_timestamp';
      END IF;
      IF NEW.execution_stage='finished'
        AND NEW.outcome IN ('cancelled','authorization_invalid')
        AND NEW.cancelled_at IS NULL THEN
        RAISE EXCEPTION 'terminal_attempt_requires_cancellation_record';
      END IF;
      NEW.updated_at := now();
      RETURN NEW;
    END $$;

    CREATE FUNCTION presales.resume_proposal_attempt(
      actor uuid,
      attempt uuid,
      expected_generation bigint,
      expected_run_revision bigint,
      expected_task_revision bigint,
      expected_input_revision bigint,
      intent_request uuid,
      lease_seconds integer
    ) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,presales,iam AS $$
    DECLARE current_attempt presales.proposal_analysis_attempts;
            current_task presales.tasks;
            resumed bigint;
    BEGIN
      PERFORM pg_advisory_xact_lock_shared(72401,1);
      IF lease_seconds IS NULL OR lease_seconds < 10 OR lease_seconds > 600
        THEN RAISE EXCEPTION 'invalid_input'; END IF;
      IF NOT EXISTS(SELECT 1 FROM iam.principals WHERE id=actor AND active AND role='architect')
        THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;

      SELECT * INTO current_attempt FROM presales.proposal_analysis_attempts
        WHERE id=attempt FOR UPDATE;
      IF NOT FOUND OR current_attempt.actor <> actor
        THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;

      IF NOT EXISTS(WITH RECURSIVE parents(id,parent_id) AS (
          SELECT id,parent_id FROM presales.resources
            WHERE id=current_attempt.task_id AND kind='task'
          UNION ALL SELECT r.id,r.parent_id FROM presales.resources r JOIN parents p ON r.id=p.parent_id
        ) SELECT 1 FROM parents p JOIN iam.resource_grants g ON g.resource_id=p.id
          WHERE g.principal_id=actor)
        THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;

      SELECT * INTO current_task FROM presales.tasks WHERE id=current_attempt.task_id FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;

      IF current_attempt.execution_stage='finished'
        OR current_attempt.cancelled_at IS NOT NULL
        OR current_task.state='cancelled'
        THEN RAISE EXCEPTION 'attempt_terminal'; END IF;
      IF current_attempt.execution_stage <> 'reconciling'
        THEN RAISE EXCEPTION 'not_ready'; END IF;
      IF current_attempt.execution_generation <> expected_generation
        THEN RAISE EXCEPTION 'fence_conflict'; END IF;
      IF current_attempt.run_revision <> expected_run_revision
        OR current_task.revision <> expected_task_revision
        THEN RAISE EXCEPTION 'revision_or_request_conflict'; END IF;
      IF current_attempt.input_revision <> expected_input_revision
        OR current_task.input_revision <> expected_input_revision
        THEN RAISE EXCEPTION 'invalid_input'; END IF;

      IF NOT EXISTS (
        SELECT 1 FROM presales.proposal_execution_records r
        WHERE r.attempt_id=attempt AND r.kind='recovery'
          AND r.record_key='recovery-intent-'||intent_request::text
          AND r.execution_generation=expected_generation
          AND r.status='completed')
        THEN RAISE EXCEPTION 'recovery_blocked'; END IF;

      IF EXISTS (
        SELECT 1 FROM presales.proposal_execution_records r
        WHERE r.attempt_id=attempt AND r.kind='call'
          AND r.status IN ('registered','unknown'))
        THEN RAISE EXCEPTION 'recovery_blocked'; END IF;

      UPDATE presales.proposal_task_slots
        SET execution_generation=expected_generation+1,owner=actor,
            lease_until=now()+(lease_seconds||' seconds')::interval
        WHERE task_id=current_attempt.task_id AND attempt_id=attempt
          AND execution_generation=expected_generation;
      GET DIAGNOSTICS resumed = ROW_COUNT;
      IF resumed <> 1 THEN RAISE EXCEPTION 'fence_conflict'; END IF;

      UPDATE presales.proposal_global_slot
        SET execution_generation=expected_generation+1,owner=actor,
            lease_until=now()+(lease_seconds||' seconds')::interval
        WHERE singleton=true AND attempt_id=attempt
          AND execution_generation=expected_generation;
      GET DIAGNOSTICS resumed = ROW_COUNT;
      IF resumed <> 1 THEN RAISE EXCEPTION 'fence_conflict'; END IF;

      UPDATE presales.proposal_analysis_attempts
        SET execution_generation=expected_generation+1,execution_stage='running',
            run_revision=run_revision+1,error_code=NULL,updated_at=now()
        WHERE id=attempt AND execution_generation=expected_generation
          AND execution_stage='reconciling';
      GET DIAGNOSTICS resumed = ROW_COUNT;
      IF resumed <> 1 THEN RAISE EXCEPTION 'fence_conflict'; END IF;
      RETURN expected_generation+1;
    END $$;
    REVOKE ALL ON FUNCTION presales.resume_proposal_attempt(
      uuid,uuid,bigint,bigint,bigint,bigint,uuid,integer) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION presales.resume_proposal_attempt(
      uuid,uuid,bigint,bigint,bigint,bigint,uuid,integer) TO presales_runtime;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
