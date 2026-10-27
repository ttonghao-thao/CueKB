-- Complete CueKB schema for an empty database. Applied once by PostgreSQL startup.


CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE document_version_status AS ENUM (
  'uploaded', 'parsing', 'needs_review', 'indexing', 'ready', 'published', 'superseded', 'failed'
);
CREATE TYPE job_status AS ENUM ('queued', 'running', 'succeeded', 'failed');
CREATE TYPE grant_role AS ENUM ('read', 'write', 'admin');

CREATE TABLE principals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  is_system_admin boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  principal_id uuid NOT NULL REFERENCES principals(id),
  key_hash char(64) NOT NULL UNIQUE,
  label text NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_bases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  content_revision bigint NOT NULL DEFAULT 0 CHECK (content_revision >= 0),
  acl_revision bigint NOT NULL DEFAULT 0 CHECK (acl_revision >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE kb_grants (
  principal_id uuid NOT NULL REFERENCES principals(id),
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  role grant_role NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(principal_id, kb_id)
);

CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);
CREATE INDEX documents_kb_visible_idx ON documents(kb_id) WHERE deleted_at IS NULL;

CREATE TABLE document_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id),
  content_sha256 char(64) NOT NULL,
  business_version text,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_uri text,
  media_type text,
  original_filename text,
  auto_publish boolean NOT NULL DEFAULT false,
  processing_profile text NOT NULL DEFAULT 'evidence-v2:25cf8010db8e18837f93d3252d50dd9fb5a735b93b6f533b34a6ee02b84b28b5'
    CHECK(processing_profile ~ '^evidence-v2:[0-9a-f]{64}$'),
  processing_snapshot jsonb NOT NULL DEFAULT '{"approved_entities":[],"chunker_revision":"evidence-v2.4","dependency_bindings":[],"max_chunk_chars":1200,"parser_revision":"evidence-v2.4","profile_revision":"evidence-v2","scope_bindings":[],"structure_revision":"evidence-v2.4"}'::jsonb
    CONSTRAINT document_versions_snapshot_object CHECK(jsonb_typeof(processing_snapshot)='object'),
  derivation_kind text NOT NULL DEFAULT 'ingest'
    CHECK(derivation_kind IN ('ingest','reprocess','restore')),
  source_version_id uuid,
  status document_version_status NOT NULL DEFAULT 'uploaded',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(id, document_id),
  CONSTRAINT document_versions_source_same_document
    FOREIGN KEY(source_version_id,document_id) REFERENCES document_versions(id,document_id),
  CONSTRAINT document_versions_source_not_self CHECK(source_version_id IS DISTINCT FROM id)
);
CREATE UNIQUE INDEX document_versions_content_processing_unique
  ON document_versions(document_id,content_sha256,processing_profile)
  WHERE derivation_kind IN ('ingest','reprocess');

CREATE TABLE document_publications (
  document_id uuid NOT NULL REFERENCES documents(id),
  scope_key text NOT NULL,
  active_version_id uuid NOT NULL,
  published_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(document_id, scope_key),
  FOREIGN KEY(active_version_id, document_id) REFERENCES document_versions(id, document_id),
  CHECK(scope_key = 'default')
);

CREATE TABLE sections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_id uuid NOT NULL REFERENCES document_versions(id),
  parent_id uuid REFERENCES sections(id),
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  title_path jsonb NOT NULL DEFAULT '[]'::jsonb,
  content text NOT NULL
);

CREATE TABLE chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id),
  section_id uuid REFERENCES sections(id),
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  source_text text NOT NULL,
  search_text text NOT NULL,
  token_count integer CHECK (token_count IS NULL OR token_count >= 0),
  source_anchor jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence_structure jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK(jsonb_typeof(evidence_structure)='object'),
  source_spans jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK(jsonb_typeof(source_spans)='array' AND jsonb_array_length(source_spans)<=64),
  content_sha256 char(64) NOT NULL,
  UNIQUE(version_id, ordinal)
);
CREATE INDEX chunks_kb_document_idx ON chunks(kb_id, document_id, version_id);
CREATE INDEX chunks_section_ordinal_idx ON chunks(version_id,section_id,ordinal);

CREATE TABLE ingestion_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  principal_id uuid NOT NULL REFERENCES principals(id),
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id),
  idempotency_key text,
  request_sha256 char(64),
  status job_status NOT NULL DEFAULT 'queued',
  stage text NOT NULL DEFAULT 'uploaded',
  progress smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  lease_owner text,
  lease_until timestamptz,
  attempt_count integer NOT NULL DEFAULT 0,
  error_code text,
  error_message text,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(principal_id, kb_id, idempotency_key)
);

CREATE TABLE outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  lease_owner text,
  lease_until timestamptz,
  attempt_count integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);
CREATE INDEX outbox_unprocessed_idx ON outbox_events(next_attempt_at, created_at)
  WHERE processed_at IS NULL;

CREATE TABLE model_configurations (
  id smallint PRIMARY KEY DEFAULT 1 CHECK(id=1),
  embedding_base_url text NOT NULL,
  embedding_api_key bytea NOT NULL,
  embedding_model text NOT NULL,
  reranker_base_url text NOT NULL DEFAULT '',
  reranker_api_key bytea,
  reranker_model text NOT NULL DEFAULT '',
  active_index text,
  embedding_dimension integer,
  revision bigint NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE entities (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  name text NOT NULL CHECK(length(name) BETWEEN 1 AND 200),
  kind text NOT NULL,
  product_model text CHECK(length(product_model) BETWEEN 1 AND 200),
  software_version text CHECK(length(software_version) BETWEEN 1 AND 200),
  UNIQUE NULLS NOT DISTINCT(kb_id,name,kind,product_model,software_version),
  UNIQUE(id,kb_id)
);
CREATE TABLE entity_aliases (
  entity_id uuid NOT NULL REFERENCES entities(id),
  alias text NOT NULL,
  PRIMARY KEY(entity_id,alias)
);
CREATE TABLE mentions (
  entity_id uuid NOT NULL REFERENCES entities(id),
  chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  PRIMARY KEY(entity_id,chunk_id)
);
CREATE INDEX mentions_chunk_idx ON mentions(chunk_id);
CREATE TABLE relation_assertions (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  subject_id uuid NOT NULL,
  object_id uuid NOT NULL,
  relation_type text NOT NULL CHECK(relation_type IN
    ('belongs_to','adjacent_to','alias_of','revises','replaces','references','depends_on','applies_to')),
  conditions jsonb NOT NULL DEFAULT '{}'::jsonb,
  valid_from timestamptz,
  valid_until timestamptz,
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

CREATE TABLE index_generations (
  id uuid PRIMARY KEY,
  index_name text NOT NULL UNIQUE,
  embedding_model text NOT NULL,
  dimension integer NOT NULL CHECK(dimension>0),
  chunking_version text NOT NULL CONSTRAINT index_generations_chunking_profile_check
    CHECK(chunking_version ~ '^evidence-v2:[0-9a-f]{64}$'
      OR chunking_version ~ '^mixed:[0-9a-f]{64}$'),
  configuration bytea NOT NULL,
  status text NOT NULL CHECK(status IN ('queued','building','ready','active','retired','failed')),
  source_revision bigint NOT NULL,
  content_revisions jsonb NOT NULL DEFAULT '{}'::jsonb,
  build_source_profiles jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK(jsonb_typeof(build_source_profiles)='object'),
  chunk_count bigint CHECK(chunk_count IS NULL OR chunk_count>=0),
  attempt_count integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_active_generation ON index_generations ((status)) WHERE status='active';
CREATE UNIQUE INDEX one_pending_generation ON index_generations ((true)) WHERE status IN ('queued','building','ready');

CREATE TABLE processing_templates (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
  name text NOT NULL,
  configuration jsonb NOT NULL CHECK(jsonb_typeof(configuration)='object'),
  UNIQUE(kb_id,name)
);

CREATE TABLE review_plans (
  id uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id),
  rule_revision text NOT NULL,
  revision integer NOT NULL DEFAULT 0 CHECK(revision>=0),
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','applied')),
  catalog jsonb NOT NULL CHECK(jsonb_typeof(catalog)='array'),
  applied_job_id uuid REFERENCES ingestion_jobs(id),
  manifest_sha256 char(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(version_id,document_id) REFERENCES document_versions(id,document_id),
  UNIQUE(version_id,rule_revision)
);
CREATE TABLE review_facts (
  id uuid PRIMARY KEY,
  plan_id uuid NOT NULL REFERENCES review_plans(id),
  source_chunk_id uuid NOT NULL REFERENCES chunks(id),
  ordinal integer NOT NULL CHECK(ordinal>=0),
  original jsonb NOT NULL CHECK(jsonb_typeof(original)='object'),
  current jsonb NOT NULL CHECK(jsonb_typeof(current)='object'),
  UNIQUE(plan_id,ordinal)
);
CREATE INDEX review_facts_plan_idx ON review_facts(plan_id,ordinal);
CREATE TABLE review_decisions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id uuid NOT NULL REFERENCES review_plans(id),
  fact_id uuid NOT NULL REFERENCES review_facts(id),
  revision integer NOT NULL CHECK(revision>0),
  principal_id uuid NOT NULL REFERENCES principals(id),
  api_key_id uuid REFERENCES api_keys(id),
  decision jsonb NOT NULL CHECK(jsonb_typeof(decision)='object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE review_selections (
  id uuid PRIMARY KEY,
  plan_id uuid NOT NULL REFERENCES review_plans(id),
  fact_ids jsonb NOT NULL CHECK(jsonb_typeof(fact_ids)='array' AND jsonb_array_length(fact_ids) BETWEEN 1 AND 10000),
  digest char(64) NOT NULL,
  revision integer NOT NULL CHECK(revision>=0),
  completed integer NOT NULL DEFAULT 0 CHECK(completed>=0),
  operation jsonb,
  principal_id uuid NOT NULL REFERENCES principals(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
