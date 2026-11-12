"""Evidence Snapshot, derived artifact dependency closure and node readiness ledger."""

from alembic import op

revision = "0011_evidence_snapshot"
down_revision = "0010_opportunity_background"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE presales.evidence_snapshots (
      artifact_id uuid PRIMARY KEY,
      attempt_id uuid NOT NULL REFERENCES presales.proposal_analysis_attempts(id),
      node_name text NOT NULL CHECK(char_length(node_name) BETWEEN 1 AND 200),
      input_revision bigint NOT NULL CHECK(input_revision > 0),
      execution_generation bigint NOT NULL CHECK(execution_generation > 0),
      reflection_revision bigint NOT NULL DEFAULT 0 CHECK(reflection_revision BETWEEN 0 AND 1),
      technical_retry bigint NOT NULL DEFAULT 0 CHECK(technical_retry BETWEEN 0 AND 2),
      state text NOT NULL CHECK(state IN ('ready','superseded')),
      output_digest text NOT NULL CHECK(output_digest ~ '^[0-9a-f]{64}$'),
      body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(attempt_id,node_name,input_revision,reflection_revision,technical_retry)
    );
    CREATE INDEX evidence_snapshot_attempt
      ON presales.evidence_snapshots(attempt_id,created_at);

    CREATE TABLE presales.evidence_snapshot_sources (
      artifact_id uuid NOT NULL REFERENCES presales.evidence_snapshots(artifact_id),
      ordinal integer NOT NULL CHECK(ordinal >= 0),
      kind text NOT NULL CHECK(kind IN ('material','mcp_background','retrieval','reflection')),
      source_version_id uuid NOT NULL REFERENCES presales.resource_versions(id),
      evidence_id uuid,
      locator jsonb NOT NULL CHECK(jsonb_typeof(locator)='object'),
      content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
      declaration text NOT NULL CHECK(declaration IN ('synthetic','owned','public_authorized')),
      applicability jsonb NOT NULL CHECK(jsonb_typeof(applicability)='object'),
      retrieval_run_id uuid REFERENCES presales.retrieval_runs(id),
      PRIMARY KEY(artifact_id,ordinal)
    );

    CREATE TABLE presales.evidence_snapshot_upstream (
      artifact_id uuid NOT NULL REFERENCES presales.evidence_snapshots(artifact_id),
      ordinal integer NOT NULL CHECK(ordinal >= 0),
      upstream_artifact_id uuid NOT NULL REFERENCES presales.evidence_snapshots(artifact_id),
      PRIMARY KEY(artifact_id,ordinal),
      CHECK(artifact_id <> upstream_artifact_id)
    );

    CREATE TABLE presales.node_readiness (
      attempt_id uuid NOT NULL REFERENCES presales.proposal_analysis_attempts(id),
      node_key text NOT NULL CHECK(char_length(node_key) BETWEEN 1 AND 400),
      execution_generation bigint NOT NULL CHECK(execution_generation > 0),
      state text NOT NULL CHECK(state IN ('pending','ready','failed','abandoned')),
      detail jsonb NOT NULL CHECK(jsonb_typeof(detail)='object'),
      updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(attempt_id,node_key)
    );

    CREATE FUNCTION presales.guard_evidence_snapshot() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_evidence_snapshot'; END IF;
      IF (to_jsonb(NEW)-'state') <> (to_jsonb(OLD)-'state') THEN
        RAISE EXCEPTION 'immutable_evidence_snapshot_content';
      END IF;
      IF OLD.state='superseded' THEN
        RAISE EXCEPTION 'terminal_evidence_snapshot_immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER evidence_snapshot_guard BEFORE UPDATE OR DELETE
      ON presales.evidence_snapshots
      FOR EACH ROW EXECUTE FUNCTION presales.guard_evidence_snapshot();

    CREATE FUNCTION presales.guard_snapshot_source() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE owner_id uuid;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_evidence_snapshot_source'; END IF;
      SELECT rv.resource_id INTO owner_id
        FROM presales.resource_versions rv WHERE rv.id=NEW.source_version_id;
      IF owner_id IS NULL THEN RAISE EXCEPTION 'missing_source_version'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER snapshot_source_guard BEFORE INSERT OR UPDATE OR DELETE
      ON presales.evidence_snapshot_sources
      FOR EACH ROW EXECUTE FUNCTION presales.guard_snapshot_source();

    CREATE FUNCTION presales.guard_snapshot_upstream() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_evidence_snapshot_upstream'; END IF;
      IF EXISTS (WITH RECURSIVE ancestors(id) AS (
          SELECT NEW.upstream_artifact_id
          UNION
          SELECT u.upstream_artifact_id FROM presales.evidence_snapshot_upstream u
          JOIN ancestors a ON u.artifact_id=a.id
        ) SELECT 1 FROM ancestors WHERE id=NEW.artifact_id) THEN
        RAISE EXCEPTION 'evidence_snapshot_cycle_detected';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER snapshot_upstream_guard BEFORE INSERT OR UPDATE
      ON presales.evidence_snapshot_upstream
      FOR EACH ROW EXECUTE FUNCTION presales.guard_snapshot_upstream();

    GRANT SELECT,INSERT ON presales.evidence_snapshots,
      presales.evidence_snapshot_sources,presales.evidence_snapshot_upstream TO presales_runtime;
    GRANT UPDATE(state) ON presales.evidence_snapshots TO presales_runtime;
    GRANT SELECT,INSERT ON presales.node_readiness TO presales_runtime;
    GRANT UPDATE(state,detail,updated_at) ON presales.node_readiness TO presales_runtime;
    GRANT SELECT ON presales.evidence_snapshots,presales.evidence_snapshot_sources,
      presales.evidence_snapshot_upstream,presales.node_readiness TO presales_maintainer;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
