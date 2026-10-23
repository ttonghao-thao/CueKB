"""Immutable processing profiles and persisted evidence structure.

Historical rows remain unknown; this migration never infers document ownership.
"""

from alembic import op

revision = "0004_evidence_processing"
down_revision = "0003_relations_generations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE document_versions
      ADD COLUMN processing_profile text NOT NULL DEFAULT 'structured-v1',
      ADD COLUMN processing_snapshot jsonb NOT NULL DEFAULT
        '{"profile_revision":"structured-v1","historical_configuration_incomplete":true}',
      ADD COLUMN derivation_kind text NOT NULL DEFAULT 'ingest'
        CHECK (derivation_kind IN ('ingest','reprocess','restore')),
      ADD COLUMN source_version_id uuid,
      ADD CONSTRAINT document_versions_source_same_document
        FOREIGN KEY(source_version_id,document_id) REFERENCES document_versions(id,document_id),
      ADD CONSTRAINT document_versions_source_not_self CHECK(source_version_id IS DISTINCT FROM id),
      ADD CONSTRAINT document_versions_snapshot_object CHECK(jsonb_typeof(processing_snapshot)='object');
    ALTER TABLE document_versions DROP CONSTRAINT document_versions_document_id_content_sha256_key;
    CREATE UNIQUE INDEX document_versions_content_processing_unique
      ON document_versions(document_id,content_sha256,processing_profile)
      WHERE derivation_kind IN ('ingest','reprocess');
    ALTER TABLE chunks
      ADD COLUMN evidence_structure jsonb NOT NULL DEFAULT '{}'
        CHECK(jsonb_typeof(evidence_structure)='object'),
      ADD COLUMN source_spans jsonb NOT NULL DEFAULT '[]'
        CHECK(jsonb_typeof(source_spans)='array' AND jsonb_array_length(source_spans)<=64);
    ALTER TABLE index_generations DROP CONSTRAINT index_generations_chunking_version_check;
    ALTER TABLE index_generations
      ADD COLUMN build_source_profiles jsonb NOT NULL DEFAULT '{}'
        CHECK(jsonb_typeof(build_source_profiles)='object'),
      ADD CONSTRAINT index_generations_chunking_profile_check
        CHECK (chunking_version='structured-v1' OR chunking_version LIKE 'evidence-v2:%'
          OR chunking_version LIKE 'mixed:%');
    """)


def downgrade() -> None:
    raise RuntimeError("Evidence processing versions are immutable; restore a backup to downgrade")
