"""Append-only, actor-bound restricted retrieval traces."""

from alembic import op

revision = "0005_retrieval"
down_revision = "0004_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE presales.retrieval_runs (
      id uuid PRIMARY KEY,
      actor uuid NOT NULL REFERENCES iam.principals(id),
      query_digest text NOT NULL,
      authorization_revision bigint NOT NULL,
      catalog_revision bigint NOT NULL,
      mode text NOT NULL CHECK(mode IN ('fake','real')),
      model_identity text NOT NULL,
      config jsonb NOT NULL,
      source_version_ids jsonb NOT NULL,
      ranking jsonb NOT NULL,
      degradation jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TRIGGER retrieval_runs_immutable BEFORE UPDATE OR DELETE ON presales.retrieval_runs
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    GRANT SELECT,INSERT ON presales.retrieval_runs TO presales_runtime;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
