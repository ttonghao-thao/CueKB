-- Additive S1 expansion. Existing publications and content remain unchanged.
CREATE TABLE document_graphs (
  version_id uuid PRIMARY KEY REFERENCES document_versions(id),
  revision text NOT NULL CHECK(revision='document-tree-1'),
  source_hash char(64) NOT NULL,
  source_version_id uuid REFERENCES document_versions(id),
  status text NOT NULL CHECK(status IN ('complete','limited')),
  issues jsonb NOT NULL CHECK(jsonb_typeof(issues)='array'),
  metadata jsonb NOT NULL CHECK(jsonb_typeof(metadata)='object'),
  digest char(64) NOT NULL,
  dependencies jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE document_nodes (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES document_graphs(version_id),
  parent_id uuid,
  kind text NOT NULL CHECK(kind IN ('document','section','paragraph','list','list_item','table','row','cell','footnote','code')),
  ordinal integer NOT NULL CHECK(ordinal>=0),
  text_content text NOT NULL,
  title_path jsonb NOT NULL,
  source_spans jsonb NOT NULL,
  parse_quality text NOT NULL CHECK(parse_quality IN ('exact','coarse','limited')),
  parser_key text,
  attributes jsonb NOT NULL,
  UNIQUE(id,version_id), UNIQUE(version_id,ordinal),
  FOREIGN KEY(parent_id,version_id) REFERENCES document_nodes(id,version_id) DEFERRABLE INITIALLY DEFERRED,
  CHECK(parent_id IS DISTINCT FROM id)
);
CREATE INDEX document_nodes_parent_idx ON document_nodes(version_id,parent_id,ordinal);
CREATE TABLE evidence_edges (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES document_graphs(version_id),
  source_id uuid NOT NULL,
  target_id uuid NOT NULL,
  type text NOT NULL CHECK(type IN ('parent_of','next_in_structure','header_for','footnote_for','continuation_of','governed_by','exception_to','references','defines','applies_to','supersedes','contradicts_candidate')),
  required boolean NOT NULL,
  provenance jsonb NOT NULL,
  FOREIGN KEY(source_id,version_id) REFERENCES document_nodes(id,version_id),
  FOREIGN KEY(target_id,version_id) REFERENCES document_nodes(id,version_id),
  CHECK(source_id<>target_id), UNIQUE(version_id,source_id,target_id,type)
);
CREATE INDEX evidence_edges_source_idx ON evidence_edges(version_id,source_id);
CREATE TABLE retrieval_units (
  chunk_id uuid PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_graphs(version_id),
  source_unit_id text,
  scope jsonb NOT NULL,
  processing_profile text NOT NULL,
  source_spans jsonb NOT NULL,
  UNIQUE(chunk_id,version_id)
);
CREATE TABLE retrieval_unit_nodes (
  chunk_id uuid NOT NULL REFERENCES retrieval_units(chunk_id) ON DELETE CASCADE,
  version_id uuid NOT NULL,
  node_id uuid NOT NULL,
  PRIMARY KEY(chunk_id,node_id),
  FOREIGN KEY(chunk_id,version_id) REFERENCES retrieval_units(chunk_id,version_id) ON DELETE CASCADE,
  FOREIGN KEY(node_id,version_id) REFERENCES document_nodes(id,version_id)
);
CREATE INDEX retrieval_unit_nodes_version_idx ON retrieval_unit_nodes(version_id,node_id);
CREATE TABLE scope_assertions (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES document_graphs(version_id),
  chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  subject_type text NOT NULL,
  status text NOT NULL CHECK(status IN ('known','unknown','ambiguous','not_applicable')),
  node_ids jsonb NOT NULL, subjects jsonb NOT NULL, conditions jsonb NOT NULL, provenance jsonb NOT NULL,
  FOREIGN KEY(chunk_id,version_id) REFERENCES retrieval_units(chunk_id,version_id) ON DELETE CASCADE
);
CREATE INDEX scope_assertions_version_idx ON scope_assertions(version_id,subject_type,status);
CREATE TABLE publication_snapshots (
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id),
  graph_digest char(64),
  processing_profile text NOT NULL,
  content_revision bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(document_id,version_id)
);

ALTER TABLE document_versions ALTER COLUMN processing_profile SET DEFAULT 'evidence-v2:17f3217777d8c3f64bee51bacfbdd2545307d6c362a27c03612cddd174ae8b55';
ALTER TABLE document_versions ALTER COLUMN processing_snapshot SET DEFAULT '{"approved_entities":[],"chunker_revision":"evidence-v2.5","dependency_bindings":[],"max_chunk_chars":1200,"parser_revision":"evidence-v2.5","profile_revision":"evidence-v2","scope_bindings":[],"structure_revision":"evidence-v2.5","window_token_budget":320,"window_tokenizer":"unicode-lexical-v1"}'::jsonb;
