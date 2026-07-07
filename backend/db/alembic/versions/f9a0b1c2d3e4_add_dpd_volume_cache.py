"""add dpd_volume_cache (server-side cache of raw DPD indication records)

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-07-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dpd_volume_cache",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ser_num", sa.BigInteger(), nullable=False),
        sa.Column("mf_dev", sa.Integer(), nullable=False),
        sa.Column("type_dev", sa.Integer(), nullable=False),
        sa.Column("ch_num", sa.Integer(), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ser_num", "mf_dev", "type_dev", "ch_num", "period_type", "day",
            name="uq_dpd_cache_device_period_day",
        ),
    )
    op.create_index("ix_dpd_cache_fetched_at", "dpd_volume_cache", ["fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_dpd_cache_fetched_at", table_name="dpd_volume_cache")
    op.drop_table("dpd_volume_cache")
