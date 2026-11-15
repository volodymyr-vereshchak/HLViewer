"""GRS consumption model: which trend lines measure a station

Revision ID: c5e7a9b1d3f6
Revises: b3d5f7a9c1e4
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "c5e7a9b1d3f6"
down_revision = "b3d5f7a9c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grs_model_station_line",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("station_id", sa.BigInteger(),
                  sa.ForeignKey("grs_model_station.id", ondelete="CASCADE"), nullable=False),
        sa.Column("line_kind", sa.String(length=16), nullable=False),
        sa.Column("line_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("station_id", "line_kind", "line_id", name="uq_grs_model_station_line"),
    )


def downgrade() -> None:
    op.drop_table("grs_model_station_line")
