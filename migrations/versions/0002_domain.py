"""Resources, provenance, task input sets, draft revisions, and file readiness."""

from alembic import op

revision = "0002_domain"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
    CREATE TABLE presales.resources (
        id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('opportunity','task','material','proposal','knowledge')),
        parent_id uuid REFERENCES presales.resources(id),
        CHECK((kind IN ('opportunity','knowledge') AND parent_id IS NULL) OR
              (kind IN ('task','material','proposal') AND parent_id IS NOT NULL))
    );
    CREATE TABLE iam.resource_grants (
        principal_id uuid NOT NULL REFERENCES iam.principals(id),
        resource_id uuid NOT NULL REFERENCES presales.resources(id),
        PRIMARY KEY(principal_id, resource_id)
    );
    CREATE TABLE presales.resource_versions (
        id uuid PRIMARY KEY, resource_id uuid NOT NULL REFERENCES presales.resources(id),
        version_no bigint NOT NULL CHECK(version_no > 0),
        source_kind text NOT NULL CHECK(source_kind IN ('original','derived')),
        declaration text CHECK(declaration IN ('synthetic','public_authorized','owned','unknown')),
        ready boolean NOT NULL DEFAULT false, dependencies_complete boolean NOT NULL DEFAULT false,
        input_manifest jsonb NOT NULL DEFAULT '[]',
        created_by uuid NOT NULL REFERENCES iam.principals(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(resource_id,version_no), UNIQUE(id,resource_id),
        CHECK(jsonb_typeof(input_manifest)='array'),
        CHECK((source_kind='original' AND declaration IS NOT NULL) OR
              (source_kind='derived' AND declaration IS NULL)),
        CHECK(NOT ready OR (dependencies_complete AND
              (source_kind='derived' OR declaration <> 'unknown')))
    );
    CREATE TABLE presales.source_locations (
        id uuid PRIMARY KEY, source_version_id uuid NOT NULL REFERENCES presales.resource_versions(id),
        locator jsonb NOT NULL CHECK(jsonb_typeof(locator)='object')
    );
    CREATE TABLE presales.opportunities (
        id uuid PRIMARY KEY REFERENCES presales.resources(id), name text NOT NULL,
        created_by uuid NOT NULL REFERENCES iam.principals(id),
        state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','inactive')),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.tasks (
        id uuid PRIMARY KEY REFERENCES presales.resources(id),
        opportunity_id uuid NOT NULL REFERENCES presales.opportunities(id),
        goal text NOT NULL, created_by uuid NOT NULL REFERENCES iam.principals(id),
        state text NOT NULL DEFAULT 'new' CHECK(state IN
          ('new','analyzing','analyzed_awaiting_submission','awaiting_review',
           'awaiting_supplementation','approved','completed','failed','cancelled')),
        revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
        input_revision bigint NOT NULL DEFAULT 1 CHECK(input_revision>0),
        candidate_version_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.materials (
        id uuid PRIMARY KEY REFERENCES presales.resources(id),
        opportunity_id uuid REFERENCES presales.opportunities(id),
        task_id uuid REFERENCES presales.tasks(id), filename text NOT NULL,
        CHECK(num_nonnulls(opportunity_id,task_id)=1)
    );
    CREATE TABLE presales.material_versions (
        id uuid PRIMARY KEY, material_id uuid NOT NULL REFERENCES presales.materials(id),
        storage_key text NOT NULL UNIQUE CHECK(storage_key ~ '^[0-9a-f]{32}\.bin$'),
        sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
        size_bytes bigint NOT NULL CHECK(size_bytes BETWEEN 0 AND 20971520),
        FOREIGN KEY(id,material_id) REFERENCES presales.resource_versions(id,resource_id)
    );
    CREATE TABLE presales.task_inputs (
        task_id uuid NOT NULL REFERENCES presales.tasks(id),
        material_version_id uuid NOT NULL REFERENCES presales.material_versions(id),
        PRIMARY KEY(task_id,material_version_id)
    );
    CREATE TABLE presales.proposal_versions (
        id uuid PRIMARY KEY REFERENCES presales.resources(id),
        task_id uuid NOT NULL REFERENCES presales.tasks(id), version_no bigint NOT NULL CHECK(version_no>0),
        state text NOT NULL DEFAULT 'draft' CHECK(state IN
            ('draft','awaiting_review','changes_requested','rejected','approved','retired')),
        current_revision_id uuid, revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
        UNIQUE(task_id,version_no), UNIQUE(id,task_id)
    );
    CREATE TABLE presales.proposal_revisions (
        id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES presales.proposal_versions(id),
        revision_no bigint NOT NULL CHECK(revision_no>0), schema_version text NOT NULL CHECK(schema_version='1.0'),
        body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),
        editor uuid NOT NULL REFERENCES iam.principals(id), created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(version_id,revision_no), UNIQUE(id,version_id),
        FOREIGN KEY(id,version_id) REFERENCES presales.resource_versions(id,resource_id)
    );
    ALTER TABLE presales.proposal_versions ADD CONSTRAINT current_revision_belongs_to_version
        FOREIGN KEY(current_revision_id,id) REFERENCES presales.proposal_revisions(id,version_id);
    ALTER TABLE presales.tasks ADD CONSTRAINT candidate_belongs_to_task
        FOREIGN KEY(candidate_version_id,id) REFERENCES presales.proposal_versions(id,task_id);
    CREATE TABLE presales.direct_dependencies (
        downstream_id uuid NOT NULL REFERENCES presales.resource_versions(id),
        upstream_id uuid NOT NULL REFERENCES presales.resource_versions(id),
        purpose text NOT NULL, PRIMARY KEY(downstream_id,upstream_id), CHECK(downstream_id<>upstream_id)
    );
    CREATE INDEX dependencies_upstream ON presales.direct_dependencies(upstream_id);
    CREATE INDEX resources_parent ON presales.resources(parent_id);
    CREATE TABLE presales.file_readiness (
        id uuid PRIMARY KEY REFERENCES presales.material_versions(id),
        state text NOT NULL CHECK(state IN ('pending','ready','failed')),
        last_error text, updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.command_receipts (
        actor uuid NOT NULL REFERENCES iam.principals(id), operation text NOT NULL, request_id uuid NOT NULL,
        parameter_digest text NOT NULL, result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(actor,operation,request_id)
    );
    CREATE TRIGGER revision_append_only BEFORE UPDATE OR DELETE ON presales.proposal_revisions
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    CREATE TRIGGER receipt_append_only BEFORE UPDATE OR DELETE ON presales.command_receipts
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    CREATE TRIGGER material_version_append_only BEFORE UPDATE OR DELETE ON presales.material_versions
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    CREATE TRIGGER resource_identity_immutable BEFORE UPDATE OR DELETE ON presales.resources
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    CREATE FUNCTION presales.validate_parent() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE parent_kind text;
    BEGIN
      IF NEW.parent_id IS NULL THEN RETURN NEW; END IF;
      SELECT kind INTO parent_kind FROM presales.resources WHERE id=NEW.parent_id;
      IF (NEW.kind='task' AND parent_kind='opportunity') OR
         (NEW.kind='proposal' AND parent_kind='task') OR
         (NEW.kind='material' AND parent_kind IN ('task','opportunity')) THEN RETURN NEW; END IF;
      RAISE EXCEPTION 'invalid_resource_parent';
    END $$;
    CREATE TRIGGER resource_parent BEFORE INSERT ON presales.resources
      FOR EACH ROW EXECUTE FUNCTION presales.validate_parent();
    CREATE FUNCTION presales.guard_version() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.ready OR (to_jsonb(NEW)-'ready'-'dependencies_complete') <>
         (to_jsonb(OLD)-'ready'-'dependencies_complete') THEN
        RAISE EXCEPTION 'immutable_source_version';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER version_immutable BEFORE UPDATE ON presales.resource_versions
      FOR EACH ROW EXECUTE FUNCTION presales.guard_version();
    CREATE FUNCTION presales.guard_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM pg_advisory_xact_lock(72401,2);
      IF EXISTS(SELECT 1 FROM presales.resource_versions WHERE id=NEW.downstream_id
                AND (ready OR source_kind='original')) THEN
        RAISE EXCEPTION 'immutable_dependency';
      END IF;
      IF EXISTS (WITH RECURSIVE ancestors(id) AS (
         SELECT NEW.upstream_id UNION SELECT d.upstream_id FROM presales.direct_dependencies d
         JOIN ancestors a ON d.downstream_id=a.id
      ) SELECT 1 FROM ancestors WHERE id=NEW.downstream_id) THEN
        RAISE EXCEPTION 'cyclic_dependency';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER dependency_guard BEFORE INSERT ON presales.direct_dependencies
      FOR EACH ROW EXECUTE FUNCTION presales.guard_dependency();
    CREATE TRIGGER dependency_immutable BEFORE UPDATE OR DELETE ON presales.direct_dependencies
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    GRANT SELECT ON iam.resource_grants TO presales_runtime;
    GRANT SELECT,INSERT,DELETE ON iam.resource_grants TO presales_maintainer;
    GRANT SELECT ON presales.resources TO presales_maintainer;
    GRANT SELECT,INSERT ON ALL TABLES IN SCHEMA presales TO presales_runtime;
    GRANT UPDATE(revision,input_revision,candidate_version_id,updated_at)
      ON presales.tasks TO presales_runtime;
    GRANT UPDATE(current_revision_id,revision) ON presales.proposal_versions TO presales_runtime;
    GRANT UPDATE(ready,dependencies_complete) ON presales.resource_versions TO presales_runtime;
    GRANT UPDATE(state,last_error,updated_at) ON presales.file_readiness TO presales_runtime;
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
