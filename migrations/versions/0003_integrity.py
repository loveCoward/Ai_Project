"""Enforce resource ownership, immutable inputs, and complete ready-source manifests."""

from alembic import op

revision = "0003_integrity"
down_revision = "0002_domain"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION presales.check_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE actual_parent uuid; actual_kind text; wanted_kind text; wanted_parent uuid;
    BEGIN
      SELECT parent_id,kind INTO actual_parent,actual_kind FROM presales.resources WHERE id=NEW.id;
      CASE TG_TABLE_NAME
        WHEN 'opportunities' THEN wanted_kind := 'opportunity'; wanted_parent := NULL;
        WHEN 'tasks' THEN wanted_kind := 'task'; wanted_parent := NEW.opportunity_id;
        WHEN 'materials' THEN wanted_kind := 'material'; wanted_parent := coalesce(NEW.task_id,NEW.opportunity_id);
        WHEN 'proposal_versions' THEN wanted_kind := 'proposal'; wanted_parent := NEW.task_id;
      END CASE;
      IF actual_kind IS DISTINCT FROM wanted_kind OR actual_parent IS DISTINCT FROM wanted_parent THEN
        RAISE EXCEPTION 'resource_owner_mismatch';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER opportunity_owner BEFORE INSERT OR UPDATE ON presales.opportunities
      FOR EACH ROW EXECUTE FUNCTION presales.check_ownership();
    CREATE TRIGGER task_owner BEFORE INSERT OR UPDATE ON presales.tasks
      FOR EACH ROW EXECUTE FUNCTION presales.check_ownership();
    CREATE TRIGGER material_owner BEFORE INSERT OR UPDATE ON presales.materials
      FOR EACH ROW EXECUTE FUNCTION presales.check_ownership();
    CREATE TRIGGER proposal_owner BEFORE INSERT OR UPDATE ON presales.proposal_versions
      FOR EACH ROW EXECUTE FUNCTION presales.check_ownership();
    CREATE FUNCTION presales.check_input_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE input_task uuid; input_opportunity uuid; target_opportunity uuid;
    BEGIN
      SELECT m.task_id,m.opportunity_id INTO input_task,input_opportunity
        FROM presales.materials m JOIN presales.material_versions v ON v.material_id=m.id
        WHERE v.id=NEW.material_version_id;
      SELECT opportunity_id INTO target_opportunity FROM presales.tasks WHERE id=NEW.task_id;
      IF input_task IS NOT NULL AND input_task=NEW.task_id THEN RETURN NEW; END IF;
      IF input_opportunity IS NOT NULL AND input_opportunity=target_opportunity THEN RETURN NEW; END IF;
      RAISE EXCEPTION 'task_input_owner_mismatch';
    END $$;
    CREATE TRIGGER input_owner BEFORE INSERT OR UPDATE ON presales.task_inputs
      FOR EACH ROW EXECUTE FUNCTION presales.check_input_owner();
    CREATE FUNCTION presales.check_ready_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE expected uuid[]; actual uuid[];
    BEGIN
      IF NOT NEW.ready THEN RETURN NEW; END IF;
      PERFORM pg_advisory_xact_lock(72401,2);
      SELECT coalesce(array_agg(value::uuid ORDER BY value::uuid),'{}') INTO expected
        FROM jsonb_array_elements_text(NEW.input_manifest);
      SELECT coalesce(array_agg(upstream_id ORDER BY upstream_id),'{}') INTO actual
        FROM presales.direct_dependencies WHERE downstream_id=NEW.id;
      IF expected <> actual OR (NEW.source_kind='derived' AND cardinality(actual)=0)
         OR (NEW.source_kind='original' AND cardinality(actual)>0) THEN
        RAISE EXCEPTION 'incomplete_source_manifest';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER ready_manifest BEFORE INSERT OR UPDATE ON presales.resource_versions
      FOR EACH ROW EXECUTE FUNCTION presales.check_ready_manifest();
    CREATE TRIGGER input_append_only BEFORE UPDATE OR DELETE ON presales.task_inputs
      FOR EACH ROW EXECUTE FUNCTION audit.immutable_record();
    """)


def downgrade():
    raise RuntimeError("Use forward repair; discard only an explicitly disposable test cluster")
