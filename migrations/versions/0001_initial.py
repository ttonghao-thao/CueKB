"""Create CueKB production schema."""

from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[2].joinpath("db/schema.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    raise RuntimeError(
        "CueKB initial schema downgrade is intentionally unsupported; restore a backup"
    )
