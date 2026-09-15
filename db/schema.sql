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
  status document_version_status NOT NULL DEFAULT 'uploaded',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id, content_sha256),
  UNIQUE(id, document_id)
);

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
  content_sha256 char(64) NOT NULL,
  UNIQUE(version_id, ordinal)
);
CREATE INDEX chunks_kb_document_idx ON chunks(kb_id, document_id, version_id);

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
