"""Located parsing and versioned knowledge governance; PGVector 0.8.6."""

from alembic import op

revision = "0004_knowledge"
down_revision = "0003_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.6';
    DO $$ BEGIN
      IF (SELECT extversion FROM pg_extension WHERE extname='vector') <> '0.8.6' THEN
        RAISE EXCEPTION 'unsupported_vector_version';
      END IF;
    END $$;
    CREATE TABLE presales.task_input_manifest (
      task_id uuid NOT NULL REFERENCES presales.tasks(id), input_revision bigint NOT NULL,
      material_version_id uuid NOT NULL REFERENCES presales.material_versions(id),
      necessity text NOT NULL CHECK(necessity IN ('required','optional','unclassified')),
      marked_by uuid NOT NULL REFERENCES iam.principals(id), created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(task_id,input_revision,material_version_id),
      FOREIGN KEY(task_id,material_version_id) REFERENCES presales.task_inputs(task_id,material_version_id)
    );
    CREATE TABLE presales.parse_jobs (
      id uuid PRIMARY KEY, source_version_id uuid NOT NULL REFERENCES presales.resource_versions(id),
      sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'), parser_version text NOT NULL,
      state text NOT NULL CHECK(state IN ('running','succeeded','failed')),
      attempt integer NOT NULL DEFAULT 1 CHECK(attempt>0), error_code text,
      lease_until timestamptz NOT NULL, created_by uuid NOT NULL REFERENCES iam.principals(id),
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(source_version_id,sha256,parser_version)
    );
    CREATE TABLE presales.parse_results (
      id uuid PRIMARY KEY REFERENCES presales.resource_versions(id), job_id uuid NOT NULL UNIQUE REFERENCES presales.parse_jobs(id),
      source_version_id uuid NOT NULL REFERENCES presales.resource_versions(id),
      document jsonb NOT NULL, character_count integer NOT NULL CHECK(character_count BETWEEN 1 AND 100000),
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,source_version_id)
    );
    CREATE TABLE presales.parse_errors (
      id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES presales.parse_jobs(id),
      attempt integer NOT NULL, code text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.knowledge_catalog (
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), revision bigint NOT NULL DEFAULT 0
    );
    INSERT INTO presales.knowledge_catalog VALUES(true,0);
    CREATE TABLE presales.knowledge_documents (
      id uuid PRIMARY KEY REFERENCES presales.resources(id), title text NOT NULL,
      created_by uuid NOT NULL REFERENCES iam.principals(id), created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.knowledge_versions (
      id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES presales.knowledge_documents(id),
      filename text NOT NULL, storage_key text NOT NULL UNIQUE, sha256 text NOT NULL, size_bytes bigint NOT NULL
        CHECK(size_bytes BETWEEN 0 AND 20971520),
      product_scope jsonb NOT NULL CHECK(jsonb_typeof(product_scope)='array' AND jsonb_array_length(product_scope)>0),
      deployment_scope jsonb NOT NULL CHECK(jsonb_typeof(deployment_scope)='array' AND jsonb_array_length(deployment_scope)>0),
      declaration text NOT NULL CHECK(declaration IN ('synthetic','owned','public_authorized')),
      state text NOT NULL DEFAULT 'candidate' CHECK(state IN ('candidate','awaiting_review','published','replaced','retired')),
      revision bigint NOT NULL DEFAULT 1, ready boolean NOT NULL DEFAULT false,
      submitted_by uuid NOT NULL REFERENCES iam.principals(id),
      FOREIGN KEY(id,document_id) REFERENCES presales.resource_versions(id,resource_id)
    );
    CREATE TABLE presales.index_builds (
      id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES presales.knowledge_versions(id),
      parse_result_id uuid NOT NULL REFERENCES presales.parse_results(id),
      state text NOT NULL CHECK(state IN ('building','ready','failed','cancelled')),
      config jsonb NOT NULL, model_identity text NOT NULL, dimensions integer NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
      mode text NOT NULL CHECK(mode IN ('fake','real')), chunk_count integer NOT NULL CHECK(chunk_count BETWEEN 0 AND 5000),
      replaces uuid REFERENCES presales.knowledge_versions(id), catalog_revision bigint NOT NULL,
      created_by uuid NOT NULL REFERENCES iam.principals(id), error_code text,
      created_at timestamptz NOT NULL DEFAULT now(), deadline timestamptz NOT NULL,
      UNIQUE(id,version_id), UNIQUE(id,model_identity,dimensions)
    );
    CREATE TABLE presales.chunks (
      id uuid PRIMARY KEY, build_id uuid NOT NULL REFERENCES presales.index_builds(id),
      source_version_id uuid NOT NULL REFERENCES presales.resource_versions(id),
      source_location_id uuid NOT NULL REFERENCES presales.source_locations(id),
      ordinal integer NOT NULL, text_content text NOT NULL, sha256 text NOT NULL,
      tokens jsonb NOT NULL, start_offset integer NOT NULL, end_offset integer NOT NULL,
      UNIQUE(build_id,ordinal), UNIQUE(id,build_id)
    );
    CREATE TABLE presales.embeddings (
      chunk_id uuid PRIMARY KEY, build_id uuid NOT NULL, model_identity text NOT NULL,
      dimensions integer NOT NULL, embedding public.vector NOT NULL,
      FOREIGN KEY(chunk_id,build_id) REFERENCES presales.chunks(id,build_id),
      FOREIGN KEY(build_id,model_identity,dimensions) REFERENCES presales.index_builds(id,model_identity,dimensions),
      CHECK(public.vector_dims(embedding)=dimensions)
    );
    CREATE TABLE presales.knowledge_reviews (
      id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES presales.knowledge_versions(id),
      version_revision bigint NOT NULL, catalog_revision bigint NOT NULL,
      build_id uuid NOT NULL REFERENCES presales.index_builds(id),
      action text NOT NULL CHECK(action IN ('publish','replace','retire')),
      replaces uuid REFERENCES presales.knowledge_versions(id), scope jsonb NOT NULL,
      submitted_by uuid NOT NULL REFERENCES iam.principals(id),
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.knowledge_decisions (
      id uuid PRIMARY KEY, review_id uuid NOT NULL UNIQUE REFERENCES presales.knowledge_reviews(id),
      reviewer uuid NOT NULL REFERENCES iam.principals(id), approved boolean NOT NULL, reason text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE presales.knowledge_directory (
      version_id uuid PRIMARY KEY REFERENCES presales.knowledge_versions(id),
      build_id uuid NOT NULL UNIQUE, FOREIGN KEY(build_id,version_id) REFERENCES presales.index_builds(id,version_id)
    );
    CREATE TABLE presales.knowledge_changes (
      id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES presales.knowledge_versions(id),
      change_type text NOT NULL, before_scope jsonb NOT NULL, after_scope jsonb NOT NULL,
      catalog_revision bigint NOT NULL, actor uuid NOT NULL REFERENCES iam.principals(id),
      reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE FUNCTION presales.guard_index_build() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.state<>'building' OR NEW.state NOT IN ('ready','failed','cancelled')
        OR (to_jsonb(NEW)-'state'-'error_code')<>(to_jsonb(OLD)-'state'-'error_code') THEN
        RAISE EXCEPTION 'immutable_or_invalid_index_transition'; END IF;
      IF NEW.state='ready' AND ((SELECT count(*) FROM presales.chunks WHERE build_id=NEW.id)<>NEW.chunk_count
        OR (SELECT count(*) FROM presales.embeddings WHERE build_id=NEW.id)<>NEW.chunk_count
        OR NEW.deadline<now()) THEN RAISE EXCEPTION 'incomplete_index_build'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER index_build_transition BEFORE UPDATE ON presales.index_builds
      FOR EACH ROW EXECUTE FUNCTION presales.guard_index_build();
    CREATE FUNCTION presales.guard_chunk_source() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM presales.index_builds b JOIN presales.source_locations l
        ON l.id=NEW.source_location_id AND l.source_version_id=NEW.source_version_id
        WHERE b.id=NEW.build_id AND b.version_id=NEW.source_version_id AND b.state='building')
        THEN RAISE EXCEPTION 'invalid_chunk_source'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER chunk_source BEFORE INSERT ON presales.chunks
      FOR EACH ROW EXECUTE FUNCTION presales.guard_chunk_source();
    CREATE FUNCTION presales.guard_knowledge_version() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' OR (to_jsonb(NEW)-'state'-'revision'-'ready'-'product_scope'-'deployment_scope') <>
          (to_jsonb(OLD)-'state'-'revision'-'ready'-'product_scope'-'deployment_scope') THEN
        RAISE EXCEPTION 'immutable_knowledge_version'; END IF;
      IF (NEW.product_scope<>OLD.product_scope OR NEW.deployment_scope<>OLD.deployment_scope)
        AND (OLD.state NOT IN ('candidate','awaiting_review') OR NEW.state<>'candidate'
             OR NEW.revision<>OLD.revision+1) THEN RAISE EXCEPTION 'invalid_scope_revision'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER knowledge_version_immutable BEFORE UPDATE OR DELETE ON presales.knowledge_versions
      FOR EACH ROW EXECUTE FUNCTION presales.guard_knowledge_version();
    CREATE FUNCTION presales.check_knowledge_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM presales.resources WHERE id=NEW.id AND kind='knowledge') THEN
        RAISE EXCEPTION 'knowledge_resource_mismatch'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER knowledge_identity BEFORE INSERT ON presales.knowledge_documents
      FOR EACH ROW EXECUTE FUNCTION presales.check_knowledge_identity();
    GRANT SELECT,INSERT ON presales.task_input_manifest,presales.parse_jobs,presales.parse_results,
      presales.parse_errors,presales.knowledge_documents,presales.knowledge_versions,presales.index_builds,
      presales.chunks,presales.embeddings,presales.knowledge_reviews,presales.knowledge_decisions,
      presales.knowledge_directory,presales.knowledge_changes TO presales_runtime;
    GRANT SELECT ON presales.knowledge_catalog TO presales_runtime;
    GRANT UPDATE(revision) ON presales.knowledge_catalog TO presales_runtime;
    CREATE FUNCTION presales.mark_material_supplementation(actor uuid, target uuid)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,presales,iam AS $$
    BEGIN
      PERFORM pg_advisory_xact_lock_shared(72401,1);
      IF NOT EXISTS(SELECT 1 FROM iam.principals WHERE id=actor AND active AND role='architect')
        OR NOT EXISTS(WITH RECURSIVE parents(id,parent_id) AS (
          SELECT id,parent_id FROM presales.resources WHERE id=target AND kind='task'
          UNION ALL SELECT r.id,r.parent_id FROM presales.resources r JOIN parents p ON r.id=p.parent_id
        ) SELECT 1 FROM parents p JOIN iam.resource_grants g ON g.resource_id=p.id WHERE g.principal_id=actor)
        THEN RAISE EXCEPTION 'not_found_or_not_allowed'; END IF;
      UPDATE presales.tasks SET state='awaiting_supplementation',revision=revision+1,updated_at=now()
        WHERE id=target AND state IN ('new','analyzing');
    END $$;
    REVOKE ALL ON FUNCTION presales.mark_material_supplementation(uuid,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION presales.mark_material_supplementation(uuid,uuid) TO presales_runtime;
    GRANT UPDATE(state,attempt,error_code,lease_until) ON presales.parse_jobs TO presales_runtime;
    GRANT UPDATE(state,revision,ready,product_scope,deployment_scope) ON presales.knowledge_versions TO presales_runtime;
    GRANT UPDATE(state,error_code) ON presales.index_builds TO presales_runtime;
    GRANT DELETE ON presales.knowledge_directory TO presales_runtime;
    """)
    for table in (
        "task_input_manifest",
        "parse_results",
        "parse_errors",
        "knowledge_documents",
        "chunks",
        "embeddings",
        "knowledge_reviews",
        "knowledge_decisions",
        "knowledge_changes",
        "source_locations",
    ):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON presales.{table}
          FOR EACH ROW EXECUTE FUNCTION audit.immutable_record()""")


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
