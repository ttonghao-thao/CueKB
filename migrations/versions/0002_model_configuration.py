"""Persist administrator-managed external model service configuration."""

from alembic import op

revision = "0002_model_configuration"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_configurations (
          id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
          embedding_base_url text NOT NULL,
          embedding_api_key bytea NOT NULL,
          embedding_model text NOT NULL,
          reranker_base_url text NOT NULL DEFAULT '',
          reranker_api_key bytea,
          reranker_model text NOT NULL DEFAULT '',
          revision bigint NOT NULL DEFAULT 1,
          updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE model_configurations")
