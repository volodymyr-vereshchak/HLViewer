"""add dpd_active_poll (ephemeral registry of in-flight DPD polls)

UNLOGGED: rows live seconds-to-minutes (registered before a real DPD poll,
deleted on completion), so WAL durability is unwanted вЂ” the table is simply
truncated when Postgres restarts.

Revision ID: b7f3d2c1e0a9
Revises: f9a0b1c2d3e4
Create Date: 2026-07-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7f3d2c1e0a9"
down_revision: Union[str, None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dpd_active_poll",
        sa.Column("lock_key", sa.String(length=64), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("window_from", sa.DateTime(), nullable=False),
        sa.Column("window_to", sa.DateTime(), nullable=False),
        sa.Column(
            "device_hashes", postgresql.ARRAY(sa.BigInteger()), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("lock_key"),
        prefixes=["UNLOGGED"],
    )


def downgrade() -> None:
    op.drop_table("dpd_active_poll")

