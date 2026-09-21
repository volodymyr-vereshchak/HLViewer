"""Remember the periods a corrector was asked for and did not have

The plan now sends an agent the holes in a card's archive, so an hour lost to
a bad line is read on the next call rather than never. A real hole — the
corrector was off and wrote nothing — would then be asked for on every call;
this table is where an agent's "read to the end, still nothing" is kept.

Revision ID: a7c3e9f1b250
Revises: e5b7c1a93d24
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa


revision = "a7c3e9f1b250"
down_revision = "e5b7c1a93d24"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("poll_gap_absent")
