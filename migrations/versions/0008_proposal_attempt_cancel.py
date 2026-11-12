"""Minimal runtime privilege to cancel an unapproved proposal task."""

from alembic import op

revision = "0008_proposal_attempt_cancel"
down_revision = "0007_proposal_attempt_start"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION presales.cancel_proposal_task(
      actor uuid, target uuid, expected_revision bigint
    ) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,presales,iam AS $$
    DECLARE changed integer; current_state text; current_revision bigint;
    BEGIN
      PERFORM pg_advisory_xact_lock_shared(72401,1);
      IF NOT EXISTS(SELECT 1 FROM iam.principals WHERE id=actor AND active AND role='architect')
        OR NOT EXISTS(WITH RECURSIVE parents(id,parent_id) AS (
          SELECT id,parent_id FROM presales.resources WHERE id=target AND kind='task'
          UNION ALL SELECT r.id,r.parent_id FROM presales.resources r JOIN parents p ON r.id=p.parent_id
        ) SELECT 1 FROM parents p JOIN iam.resource_grants g ON g.resource_id=p.id WHERE g.principal_id=actor)
        THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;
      SELECT state,revision INTO current_state,current_revision
        FROM presales.tasks WHERE id=target FOR UPDATE;
      IF NOT FOUND OR current_revision <> expected_revision
        THEN RAISE EXCEPTION 'revision_or_request_conflict'; END IF;
      IF current_state IN ('approved','completed')
        THEN RAISE EXCEPTION 'cancel_not_allowed'; END IF;
      IF current_state='cancelled'
        THEN RAISE EXCEPTION 'attempt_terminal'; END IF;
      IF current_state NOT IN ('new','analyzing','analyzed_awaiting_submission',
        'awaiting_supplementation','failed','awaiting_review')
        THEN RAISE EXCEPTION 'cancel_not_allowed'; END IF;
      UPDATE presales.tasks SET state='cancelled',revision=revision+1,updated_at=now()
        WHERE id=target;
      GET DIAGNOSTICS changed = ROW_COUNT;
      IF changed <> 1 THEN RAISE EXCEPTION 'revision_or_request_conflict'; END IF;
      RETURN current_revision+1;
    END $$;
    REVOKE ALL ON FUNCTION presales.cancel_proposal_task(uuid,uuid,bigint) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION presales.cancel_proposal_task(uuid,uuid,bigint) TO presales_runtime;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
