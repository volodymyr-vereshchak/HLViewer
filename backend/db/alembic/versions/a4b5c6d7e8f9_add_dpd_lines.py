"""DPD lines: lines fed from the DPD API with a device (corrector) history.

Creates dpd_line (id drawn from shared_line_id_seq so DPD line IDs never
collide with physical/virtual line IDs), dpd_line_device (device history —
each entry's validity window is [installed_from, next entry's
installed_from), derived, not stored), dpd_line_daily_archive /
dpd_line_hourly_archive (permanent per-line archives, no retention) and
dpd_line_job (per-line init/update lock + progress).

Revision ID: a4b5c6d7e8f9
Revises: d9e0f1a2b3c4
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "dpd_line",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("branch_id", sa.BigInteger(), nullable=False),
        sa.Column("lumg_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("include_in_trends", sa.Boolean(), nullable=False,
                  server_default="false"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["branch_id"], ["grmu_branch.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lumg_id"], ["lumg.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("branch_id", "name", name="uq_dpd_line_branch_name"),
    )
    op.create_index("ix_dpd_line_branch", "dpd_line", ["branch_id"])
    # DPD lines share the line ID space with physical and virtual lines
    # (see c2d3e4f5a6b7): the frontend keys name maps and chart series by
    # bare line_id, so IDs must never collide across the three kinds.
    op.execute(
        "ALTER TABLE dpd_line "
        "ALTER COLUMN id SET DEFAULT nextval('shared_line_id_seq')"
    )

    op.create_table(
        "dpd_line_device",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=False),
        sa.Column("ser_num", sa.BigInteger(), nullable=False),
        sa.Column("corector_type_id", sa.BigInteger(), nullable=False),
        sa.Column("ch_num", sa.Integer(), nullable=False),
        sa.Column("installed_from", sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["dpd_line_id"], ["dpd_line.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["corector_type_id"], ["corector_type.id"],
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("dpd_line_id", "installed_from",
                            name="uq_dpd_line_device_from"),
    )
    op.create_index("ix_dpd_line_device_line", "dpd_line_device", ["dpd_line_id"])

    op.create_table(
        "dpd_line_daily_archive",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("pressure", sa.Float(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("press_unit", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["dpd_line_id"], ["dpd_line.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("dpd_line_id", "day", name="uq_dpd_line_daily"),
    )
    op.create_index("ix_dpd_line_daily_day", "dpd_line_daily_archive", ["day"])

    op.create_table(
        "dpd_line_hourly_archive",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=False),
        sa.Column("stamp", sa.DateTime(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("pressure", sa.Float(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("press_unit", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["dpd_line_id"], ["dpd_line.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("dpd_line_id", "stamp", name="uq_dpd_line_hourly"),
    )
    op.create_index("ix_dpd_line_hourly_stamp", "dpd_line_hourly_archive",
                    ["stamp"])

    op.create_table(
        "dpd_line_job",
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False,
                  server_default="init"),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="idle"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("progress_done", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("dpd_line_id"),
        sa.ForeignKeyConstraint(["dpd_line_id"], ["dpd_line.id"],
                                ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("dpd_line_job")
    op.drop_index("ix_dpd_line_hourly_stamp", table_name="dpd_line_hourly_archive")
    op.drop_table("dpd_line_hourly_archive")
    op.drop_index("ix_dpd_line_daily_day", table_name="dpd_line_daily_archive")
    op.drop_table("dpd_line_daily_archive")
    op.drop_index("ix_dpd_line_device_line", table_name="dpd_line_device")
    op.drop_table("dpd_line_device")
    op.drop_index("ix_dpd_line_branch", table_name="dpd_line")
    op.drop_table("dpd_line")
