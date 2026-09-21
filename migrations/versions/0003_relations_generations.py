"""Evidence-backed relations and index generation control."""

from alembic import op

revision = "0003_relations_generations"
down_revision = "0002_model_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE entities (
      id uuid PRIMARY KEY, kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
      name text NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
      kind text NOT NULL, UNIQUE(kb_id,name,kind), UNIQUE(id,kb_id)
    );
    CREATE TABLE entity_aliases (
      entity_id uuid NOT NULL REFERENCES entities(id), alias text NOT NULL,
      PRIMARY KEY(entity_id,alias)
    );
    CREATE TABLE mentions (
      entity_id uuid NOT NULL REFERENCES entities(id), chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
      PRIMARY KEY(entity_id,chunk_id)
    );
    CREATE INDEX mentions_chunk_idx ON mentions(chunk_id);
    CREATE TABLE relation_assertions (
      id uuid PRIMARY KEY, kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
      subject_id uuid NOT NULL, object_id uuid NOT NULL,
      relation_type text NOT NULL CHECK(relation_type IN
        ('belongs_to','adjacent_to','alias_of','revises','replaces','references','depends_on','applies_to')),
      conditions jsonb NOT NULL DEFAULT '{}', valid_from timestamptz, valid_until timestamptz,
      FOREIGN KEY(subject_id,kb_id) REFERENCES entities(id,kb_id),
      FOREIGN KEY(object_id,kb_id) REFERENCES entities(id,kb_id),
      CHECK(subject_id<>object_id),
      CHECK(valid_until IS NULL OR valid_from IS NULL OR valid_until>valid_from)
    );
    CREATE INDEX relations_subject_idx ON relation_assertions(kb_id,subject_id);
    CREATE INDEX relations_object_idx ON relation_assertions(kb_id,object_id);
    CREATE TABLE relation_evidence (
      relation_id uuid NOT NULL REFERENCES relation_assertions(id) ON DELETE CASCADE,
      chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
      stance text NOT NULL CHECK(stance IN ('supports','refutes')),
      PRIMARY KEY(relation_id,chunk_id,stance)
    );
    CREATE INDEX relation_evidence_chunk_idx ON relation_evidence(chunk_id);
    -- Backfill existing authoritative chunks without reparsing or changing their IDs.
    WITH paths AS (
      SELECT c.version_id,c.ordinal,
        COALESCE((SELECT jsonb_agg(h.value ORDER BY h.n) FROM jsonb_array_elements(
          COALESCE(c.source_anchor->'heading_path','[]'::jsonb)) WITH ORDINALITY h(value,n)
          WHERE h.n<=depth),'[]'::jsonb) path
      FROM chunks c CROSS JOIN LATERAL generate_series(0,jsonb_array_length(
        COALESCE(c.source_anchor->'heading_path','[]'::jsonb))) depth
      WHERE c.section_id IS NULL
    )
    INSERT INTO sections(id,version_id,parent_id,ordinal,title_path,content)
    SELECT md5(version_id::text||':'||path::text)::uuid,version_id,
      CASE WHEN jsonb_array_length(path)=0 THEN NULL ELSE
        md5(version_id::text||':'||(path - (jsonb_array_length(path)-1))::text)::uuid END,
      min(ordinal),path,'' FROM paths GROUP BY version_id,path;
    UPDATE chunks SET section_id=md5(version_id::text||':'||COALESCE(source_anchor->'heading_path','[]'::jsonb)::text)::uuid
      WHERE section_id IS NULL;
    UPDATE sections s SET content=x.content FROM (
      SELECT section_id,string_agg(source_text,E'\\n\\n' ORDER BY ordinal) content
      FROM chunks GROUP BY section_id
    ) x WHERE s.id=x.section_id;
    CREATE INDEX chunks_section_ordinal_idx ON chunks(version_id,section_id,ordinal);
    ALTER TABLE model_configurations ADD COLUMN active_index text;
    ALTER TABLE model_configurations ADD COLUMN embedding_dimension integer;
    CREATE TABLE index_generations (
      id uuid PRIMARY KEY, index_name text NOT NULL UNIQUE,
      embedding_model text NOT NULL, dimension integer NOT NULL CHECK(dimension>0),
      chunking_version text NOT NULL CHECK(chunking_version='structured-v1'),
      configuration bytea NOT NULL,
      status text NOT NULL CHECK(status IN ('queued','building','ready','active','retired','failed')),
      source_revision bigint NOT NULL, content_revisions jsonb NOT NULL DEFAULT '{}',
      chunk_count bigint CHECK(chunk_count IS NULL OR chunk_count>=0), attempt_count integer NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      error_code text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX one_active_generation ON index_generations ((status)) WHERE status='active';
    CREATE UNIQUE INDEX one_pending_generation ON index_generations ((true)) WHERE status IN ('queued','building','ready');
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE index_generations;
    ALTER TABLE model_configurations DROP COLUMN active_index, DROP COLUMN embedding_dimension;
    DROP INDEX chunks_section_ordinal_idx;
    DROP TABLE relation_evidence,relation_assertions,mentions,entity_aliases,entities;
    """)
