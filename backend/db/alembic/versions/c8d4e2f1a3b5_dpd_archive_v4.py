"""DPD archive v4: scheduler-fed normalized archive tables replace the
request-driven JSONB cache.

Drops dpd_volume_cache and dpd_active_poll; creates dpd_daily_archive,
dpd_hourly_archive (normalized per-record rows keyed by enterprise_id FK),
dpd_device_coverage (how far back each device was ever fetched) and
dpd_refresh_job (single-row scheduler/manual refresh lock, seeded id=1).

Revision ID: c8d4e2f1a3b5
Revises: b7f3d2c1e0a9
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c8d4e2f1a3b5"
down_revision: Union[str, None] = "b7f3d2c1e0a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _archive_columns():
    return [
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("dvst_alwrk", sa.Float(), nullable=True),
        sa.Column("dvwrk_alwrk", sa.Float(), nullable=True),
        sa.Column("press", sa.Float(), nullable=True),
        sa.Column("temper", sa.Float(), nullable=True),
        sa.Column("press_unit", sa.String(length=16), nullable=True),
        sa.Column("accessed_at", sa.Date(), nullable=False),
    ]


def upgrade() -> None:
    op.drop_index("ix_dpd_cache_fetched_at", table_name="dpd_volume_cache")
    op.drop_table("dpd_volume_cache")
    op.drop_table("dpd_active_poll")

    op.create_table(
        "dpd_daily_archive",
        *_archive_columns(),
        sa.Column("day", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprise.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("enterprise_id", "day", name="uq_dpd_daily_ent_day"),
    )
    op.create_index("ix_dpd_daily_day", "dpd_daily_archive", ["day"])

    op.create_table(
        "dpd_hourly_archive",
        *_archive_columns(),
        sa.Column("stamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprise.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("enterprise_id", "stamp", name="uq_dpd_hourly_ent_stamp"),
    )
    op.create_index("ix_dpd_hourly_stamp", "dpd_hourly_archive", ["stamp"])

    op.create_table(
        "dpd_device_coverage",
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("loaded_from", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("enterprise_id", "period_type"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprise.id"], ondelete="CASCADE"
        ),
    )

    op.create_table(
        "dpd_refresh_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="idle"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO dpd_refresh_job (id, status) VALUES (1, 'idle')")


def downgrade() -> None:
    op.drop_table("dpd_refresh_job")
    op.drop_table("dpd_device_coverage")
    op.drop_index("ix_dpd_hourly_stamp", table_name="dpd_hourly_archive")
    op.drop_table("dpd_hourly_archive")
    op.drop_index("ix_dpd_daily_day", table_name="dpd_daily_archive")
    op.drop_table("dpd_daily_archive")

    # Recreate the old cache tables empty (structure from f9a0b1c2d3e4 /
    # b7f3d2c1e0a9) so a downgrade leaves a working pre-v4 schema.
    op.create_table(
        "dpd_volume_cache",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ser_num", sa.BigInteger(), nullable=False),
        sa.Column("mf_dev", sa.Integer(), nullable=False),
        sa.Column("type_dev", sa.Integer(), nullable=False),
        sa.Column("ch_num", sa.Integer(), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="[]"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ser_num", "mf_dev", "type_dev", "ch_num", "period_type", "day",
            name="uq_dpd_cache_device_period_day",
        ),
    )
    op.create_index("ix_dpd_cache_fetched_at", "dpd_volume_cache", ["fetched_at"])
    op.create_table(
        "dpd_active_poll",
        sa.Column("lock_key", sa.String(length=64), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("window_from", sa.DateTime(), nullable=False),
        sa.Column("window_to", sa.DateTime(), nullable=False),
        sa.Column("device_hashes", postgresql.ARRAY(sa.BigInteger()),
                  nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("lock_key"),
        prefixes=["UNLOGGED"],
    )
