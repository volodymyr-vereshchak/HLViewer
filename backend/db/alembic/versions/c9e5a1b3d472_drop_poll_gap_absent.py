"""Drop poll_gap_absent

It remembered "the corrector does not have this period" so that a hole the
corrector could not fill was not asked for on every call. It could not be
checked against anything, and one wrong conclusion — agent 0.10.0 skipped the
period of a reset Тандем counter, which looked exactly like a period the
corrector did not have — kept Миколай-Поле's 14.09 empty for good.

The agent now asks only for what a corrector can hold: up to where its own
archive starts, or, where that cannot be learned, only between the oldest and
newest records we hold. A real hole is simply asked for again.

Revision ID: c9e5a1b3d472
Revises: a7c3e9f1b250
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa


revision = "c9e5a1b3d472"
down_revision = "a7c3e9f1b250"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("poll_gap_absent")


def downgrade() -> None:
    op.create_table(
        "poll_gap_absent",
        sa.Column("poll_device_id", sa.BigInteger(), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("stamp", sa.DateTime(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["poll_device_id"], ["poll_device.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("poll_device_id", "period_type", "stamp"),
    )
