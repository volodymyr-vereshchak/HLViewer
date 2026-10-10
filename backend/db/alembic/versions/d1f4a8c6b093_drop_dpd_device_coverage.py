"""Drop dpd_device_coverage

The table recorded how far back each device had ever been fetched from DPD, so
a read asking for an older range could backfill just the missing head. That
reader was removed when enterprise volumes started being served from the
archive alone, and the prune that used to raise coverage went with retention.
What was left only ever wrote to it: nothing had read loaded_from since.

The refresh now carries each device on from its own newest stored period,
which the archive itself answers, so there is nothing left for this table to
say.

Revision ID: d1f4a8c6b093
Revises: c9e5a1b3d472
Create Date: 2026-10-10
"""
import sqlalchemy as sa
from alembic import op

revision = "d1f4a8c6b093"
down_revision = "c9e5a1b3d472"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("dpd_device_coverage")


def downgrade() -> None:
    op.create_table(
        "dpd_device_coverage",
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("loaded_from", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"], ["dpd_device.id"], ondelete="CASCADE",
            name="dpd_device_coverage_device_id_fkey",
        ),
        sa.PrimaryKeyConstraint("device_id", "period_type"),
    )
