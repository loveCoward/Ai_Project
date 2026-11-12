"""Opportunity background and history records registered as traceable sources."""

from alembic import op

revision = "0010_opportunity_background"
down_revision = "0009_proposal_attempt_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE presales.opportunity_background_records (
      id uuid PRIMARY KEY,
      opportunity_id uuid NOT NULL REFERENCES presales.opportunities(id),
      source_version_id uuid NOT NULL UNIQUE REFERENCES presales.resource_versions(id),
      kind text NOT NULL CHECK(kind IN ('background','history')),
      title text NOT NULL CHECK(char_length(title) BETWEEN 1 AND 500),
      body text NOT NULL CHECK(char_length(body) BETWEEN 1 AND 20000),
      declaration text NOT NULL CHECK(declaration IN ('synthetic','owned','public_authorized')),
      trust_state text NOT NULL DEFAULT 'confirmed'
        CHECK(trust_state IN ('confirmed','pending_confirmation','expired','conflicting')),
      effective_at timestamptz NOT NULL,
      created_by uuid NOT NULL REFERENCES iam.principals(id),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(id,opportunity_id)
    );
    CREATE INDEX opportunity_background_lookup
      ON presales.opportunity_background_records(opportunity_id,trust_state,effective_at);

    CREATE FUNCTION presales.guard_opportunity_background() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE owner_presales uuid;
            owner_kind text;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable_opportunity_background'; END IF;
      IF (to_jsonb(NEW)-'trust_state')
          <> (to_jsonb(OLD)-'trust_state') THEN
        RAISE EXCEPTION 'immutable_opportunity_background_identity';
      END IF;
      IF OLD.trust_state IN ('expired','conflicting')
        AND NEW.trust_state NOT IN ('expired','conflicting') THEN
        RAISE EXCEPTION 'terminal_opportunity_background_trust_immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER opportunity_background_guard BEFORE UPDATE OR DELETE
      ON presales.opportunity_background_records
      FOR EACH ROW EXECUTE FUNCTION presales.guard_opportunity_background();

    CREATE FUNCTION presales.check_background_source_owner() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE owner_id uuid;
            owner_kind text;
    BEGIN
      SELECT rv.resource_id INTO owner_id
        FROM presales.resource_versions rv WHERE rv.id=NEW.source_version_id;
      SELECT r.kind INTO owner_kind
        FROM presales.resources r WHERE r.id=owner_id;
      IF owner_kind IS DISTINCT FROM 'opportunity' OR owner_id IS DISTINCT FROM NEW.opportunity_id THEN
        RAISE EXCEPTION 'background_source_owner_mismatch';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER background_source_owner BEFORE INSERT OR UPDATE
      ON presales.opportunity_background_records
      FOR EACH ROW EXECUTE FUNCTION presales.check_background_source_owner();

    GRANT SELECT,INSERT ON presales.opportunity_background_records TO presales_runtime;
    GRANT UPDATE(trust_state) ON presales.opportunity_background_records TO presales_runtime;
    GRANT SELECT ON presales.opportunity_background_records TO presales_maintainer;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
