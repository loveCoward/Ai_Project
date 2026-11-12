"""Minimal runtime privilege to begin proposal analysis on a presales task."""

from alembic import op

revision = "0007_proposal_attempt_start"
down_revision = "0006_proposal_attempt"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION presales.begin_proposal_analysis(actor uuid, target uuid)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,presales,iam AS $$
    DECLARE changed integer;
    BEGIN
      PERFORM pg_advisory_xact_lock_shared(72401,1);
      IF NOT EXISTS(SELECT 1 FROM iam.principals WHERE id=actor AND active AND role='architect')
        OR NOT EXISTS(WITH RECURSIVE parents(id,parent_id) AS (
          SELECT id,parent_id FROM presales.resources WHERE id=target AND kind='task'
          UNION ALL SELECT r.id,r.parent_id FROM presales.resources r JOIN parents p ON r.id=p.parent_id
        ) SELECT 1 FROM parents p JOIN iam.resource_grants g ON g.resource_id=p.id WHERE g.principal_id=actor)
        THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;
      UPDATE presales.tasks SET state='analyzing',revision=revision+1,updated_at=now()
        WHERE id=target AND state IN ('new','awaiting_supplementation','failed',
          'analyzed_awaiting_submission');
      GET DIAGNOSTICS changed = ROW_COUNT;
      IF changed <> 1 THEN RAISE EXCEPTION 'revision_or_request_conflict'; END IF;
    END $$;
    REVOKE ALL ON FUNCTION presales.begin_proposal_analysis(uuid,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION presales.begin_proposal_analysis(uuid,uuid) TO presales_runtime;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
