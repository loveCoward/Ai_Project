"""Proposal Analysis Attempt records, two-level slots, and write guards."""

from alembic import op

revision = "0006_proposal_attempt"
down_revision = "0005_retrieval"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE presales.proposal_analysis_attempts (
      id uuid PRIMARY KEY,
      task_id uuid NOT NULL REFERENCES presales.tasks(id),
      actor uuid NOT NULL REFERENCES iam.principals(id),
      thread_id text NOT NULL UNIQUE CHECK(char_length(thread_id) BETWEEN 1 AND 200),
      input_revision bigint NOT NULL CHECK(input_revision > 0),
      start_scope jsonb NOT NULL CHECK(
        jsonb_typeof(start_scope)='array' AND jsonb_array_length(start_scope)>0
      ),
      model_mode text NOT NULL CHECK(model_mode IN ('fake','real')),
      config_identity text NOT NULL CHECK(char_length(config_identity) BETWEEN 1 AND 200),
      execution_generation bigint NOT NULL DEFAULT 1 CHECK(execution_generation > 0),
      execution_stage text NOT NULL DEFAULT 'accepted'
        CHECK(execution_stage IN ('accepted','running','waiting_human','reconciling','finished')),
      outcome text CHECK(outcome IN
        ('succeeded','failed','materials_required','cancelled','authorization_invalid')),
      run_revision bigint NOT NULL DEFAULT 0 CHECK(run_revision >= 0),
      result_ready boolean NOT NULL DEFAULT false,
      result_revision_id uuid REFERENCES presales.proposal_revisions(id),
      candidate_version_id uuid,
      source_snapshot jsonb CHECK(
        source_snapshot IS NULL OR jsonb_typeof(source_snapshot)='array'
      ),
      cancelled_at timestamptz,
      terminal_at timestamptz,
      error_code text,
      budget jsonb NOT NULL CHECK(jsonb_typeof(budget)='object'),
      reflection_revision smallint NOT NULL DEFAULT 0 CHECK(reflection_revision BETWEEN 0 AND 1),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK((execution_stage='finished') = (outcome IS NOT NULL)),
      CHECK((execution_stage='finished') = (terminal_at IS NOT NULL)),
      CHECK(result_revision_id IS NULL OR result_ready),
      CHECK(NOT result_ready OR result_revision_id IS NOT NULL),
      FOREIGN KEY(candidate_version_id,task_id)
        REFERENCES presales.proposal_versions(id,task_id),
      FOREIGN KEY(result_revision_id,candidate_version_id)
        REFERENCES presales.proposal_revisions(id,version_id),
      CHECK(NOT result_ready OR candidate_version_id IS NOT NULL),
      UNIQUE(id,task_id)
    );
    CREATE UNIQUE INDEX one_active_attempt_per_task
      ON presales.proposal_analysis_attempts(task_id)
      WHERE execution_stage <> 'finished';
    CREATE INDEX proposal_attempt_task_created
      ON presales.proposal_analysis_attempts(task_id,created_at);
    CREATE TABLE presales.proposal_execution_records (
      attempt_id uuid NOT NULL REFERENCES presales.proposal_analysis_attempts(id),
      kind text NOT NULL CHECK(kind IN ('call','recovery','node_result')),
      record_key text NOT NULL CHECK(char_length(record_key) BETWEEN 1 AND 200),
      execution_generation bigint NOT NULL CHECK(execution_generation > 0),
      input_digest text NOT NULL CHECK(input_digest ~ '^[0-9a-f]{64}$'),
      status text NOT NULL CHECK(status IN ('registered','unknown','completed','failed')),
      body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),
      result_revision_id uuid REFERENCES presales.proposal_revisions(id),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(attempt_id,kind,record_key)
    );
    CREATE TABLE presales.proposal_checkpoint_receipts (
      attempt_id uuid NOT NULL REFERENCES presales.proposal_analysis_attempts(id),
      execution_generation bigint NOT NULL CHECK(execution_generation > 0),
      run_revision bigint NOT NULL CHECK(run_revision >= 0),
      operation text NOT NULL CHECK(operation IN ('checkpoint','pending_writes')),
      checkpoint_id text NOT NULL,
      write_task_id text NOT NULL,
      checked_at timestamptz NOT NULL,
      PRIMARY KEY(attempt_id,execution_generation,operation,checkpoint_id,write_task_id)
    );
    CREATE TABLE presales.proposal_task_slots (
      task_id uuid PRIMARY KEY REFERENCES presales.tasks(id),
      attempt_id uuid NOT NULL UNIQUE REFERENCES presales.proposal_analysis_attempts(id),
      execution_generation bigint NOT NULL CHECK(execution_generation > 0),
      owner uuid NOT NULL REFERENCES iam.principals(id),
      lease_until timestamptz NOT NULL,
      acquired_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.proposal_global_slot (
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
      attempt_id uuid UNIQUE REFERENCES presales.proposal_analysis_attempts(id),
      execution_generation bigint CHECK(execution_generation > 0),
      owner uuid REFERENCES iam.principals(id),
      lease_until timestamptz,
      CHECK(
        (attempt_id IS NULL AND execution_generation IS NULL
          AND owner IS NULL AND lease_until IS NULL)
        OR (attempt_id IS NOT NULL AND execution_generation IS NOT NULL
          AND owner IS NOT NULL AND lease_until IS NOT NULL)
      )
    );
    INSERT INTO presales.proposal_global_slot(singleton) VALUES(true);
    CREATE FUNCTION presales.guard_proposal_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
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
    CREATE TRIGGER proposal_attempt_guard BEFORE UPDATE OR DELETE
      ON presales.proposal_analysis_attempts
      FOR EACH ROW EXECUTE FUNCTION presales.guard_proposal_attempt();
    GRANT SELECT,INSERT ON presales.proposal_analysis_attempts,
      presales.proposal_task_slots TO presales_runtime;
    GRANT SELECT,INSERT ON presales.proposal_global_slot TO presales_runtime;
    GRANT UPDATE(execution_generation,execution_stage,outcome,run_revision,result_ready,result_revision_id,
      candidate_version_id,source_snapshot,cancelled_at,terminal_at,error_code,updated_at)
      ON presales.proposal_analysis_attempts TO presales_runtime;
    GRANT UPDATE(owner,lease_until) ON presales.proposal_task_slots TO presales_runtime;
    GRANT DELETE ON presales.proposal_task_slots TO presales_runtime;
    GRANT UPDATE(attempt_id,execution_generation,owner,lease_until)
      ON presales.proposal_global_slot TO presales_runtime;
    GRANT SELECT ON presales.proposal_analysis_attempts,
      presales.proposal_task_slots,presales.proposal_global_slot TO presales_maintainer;
    GRANT SELECT,INSERT ON presales.proposal_execution_records,
      presales.proposal_checkpoint_receipts TO presales_runtime;
    GRANT UPDATE(status,body,result_revision_id,updated_at)
      ON presales.proposal_execution_records TO presales_runtime;
    GRANT SELECT ON presales.proposal_execution_records,
      presales.proposal_checkpoint_receipts TO presales_maintainer;

    CREATE FUNCTION presales.guard_proposal_execution_record() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE current_attempt presales.proposal_analysis_attempts;
    BEGIN
      SELECT * INTO current_attempt FROM presales.proposal_analysis_attempts
        WHERE id=NEW.attempt_id FOR SHARE;
      IF current_attempt.execution_generation IS DISTINCT FROM NEW.execution_generation
        OR current_attempt.execution_stage='finished' OR current_attempt.cancelled_at IS NOT NULL THEN
        RAISE EXCEPTION 'invalid_proposal_execution_generation';
      END IF;
      IF TG_OP='UPDATE' AND (
        OLD.status IN ('completed','failed')
        OR (to_jsonb(NEW)-'status'-'body'-'result_revision_id'-'updated_at')
          <> (to_jsonb(OLD)-'status'-'body'-'result_revision_id'-'updated_at')
      ) THEN RAISE EXCEPTION 'immutable_proposal_execution_record'; END IF;
      NEW.updated_at := now();
      RETURN NEW;
    END $$;
    CREATE TRIGGER proposal_execution_record_guard BEFORE INSERT OR UPDATE
      ON presales.proposal_execution_records
      FOR EACH ROW EXECUTE FUNCTION presales.guard_proposal_execution_record();

    CREATE FUNCTION presales.guard_proposal_checkpoint_receipt() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE current_attempt presales.proposal_analysis_attempts;
    BEGIN
      SELECT * INTO current_attempt FROM presales.proposal_analysis_attempts
        WHERE id=NEW.attempt_id FOR SHARE;
      IF current_attempt.execution_generation IS DISTINCT FROM NEW.execution_generation
        OR current_attempt.run_revision IS DISTINCT FROM NEW.run_revision
        OR current_attempt.execution_stage='finished' OR current_attempt.cancelled_at IS NOT NULL THEN
        RAISE EXCEPTION 'invalid_proposal_checkpoint_receipt_generation';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER proposal_checkpoint_receipt_guard BEFORE INSERT
      ON presales.proposal_checkpoint_receipts
      FOR EACH ROW EXECUTE FUNCTION presales.guard_proposal_checkpoint_receipt();
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
