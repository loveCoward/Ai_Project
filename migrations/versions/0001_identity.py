"""Identity, authorization revision, and append-only audit."""

from alembic import op

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE SCHEMA iam;
    CREATE SCHEMA presales;
    CREATE SCHEMA audit;
    CREATE TABLE iam.principals (
        id uuid PRIMARY KEY, display_name text NOT NULL,
        role text NOT NULL CHECK (role IN ('sales','architect','reviewer')),
        active boolean NOT NULL DEFAULT true
    );
    CREATE TABLE iam.authorization_revision (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        revision bigint NOT NULL DEFAULT 0 CHECK(revision >= 0)
    );
    INSERT INTO iam.authorization_revision VALUES(true, 0);
    CREATE TABLE iam.initialization (
        singleton boolean PRIMARY KEY CHECK(singleton),
        operator text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE iam.authorization_changes (
        id uuid PRIMARY KEY, operator text NOT NULL, base_revision bigint NOT NULL,
        changes jsonb NOT NULL, before_values jsonb NOT NULL, digest text NOT NULL,
        expansion boolean NOT NULL, applied_revision bigint, reason text,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE audit.events (
        id uuid PRIMARY KEY, actor text NOT NULL, operation text NOT NULL,
        target uuid, revision bigint, result text NOT NULL, request_id uuid,
        authorization_revision bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE FUNCTION audit.immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable_record'; END $$;
    CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit.events
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    DO $$ DECLARE invalid_role boolean;
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='presales_runtime') THEN
        CREATE ROLE presales_runtime NOLOGIN;
      END IF;
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='presales_maintainer') THEN
        CREATE ROLE presales_maintainer NOLOGIN;
      END IF;
      SELECT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls
        INTO invalid_role FROM pg_roles WHERE rolname='presales_runtime';
      IF invalid_role OR EXISTS(
        SELECT 1 FROM pg_auth_members WHERE member='presales_runtime'::regrole
      ) THEN RAISE EXCEPTION 'unsafe_presales_runtime_role'; END IF;
      SELECT rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls
        INTO invalid_role FROM pg_roles WHERE rolname='presales_maintainer';
      IF invalid_role OR EXISTS(
        SELECT 1 FROM pg_auth_members WHERE member='presales_maintainer'::regrole
      ) THEN RAISE EXCEPTION 'unsafe_presales_maintainer_role'; END IF;
    END $$;
    REVOKE ALL ON SCHEMA iam,presales,audit FROM PUBLIC;
    GRANT USAGE ON SCHEMA iam,presales,audit TO presales_runtime,presales_maintainer;
    GRANT SELECT ON iam.principals,iam.authorization_revision TO presales_runtime;
    GRANT SELECT ON ALL TABLES IN SCHEMA iam TO presales_maintainer;
    GRANT UPDATE(active,role) ON iam.principals TO presales_maintainer;
    GRANT UPDATE(revision) ON iam.authorization_revision TO presales_maintainer;
    GRANT INSERT,UPDATE ON iam.authorization_changes TO presales_maintainer;
    GRANT INSERT ON audit.events TO presales_runtime,presales_maintainer;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
