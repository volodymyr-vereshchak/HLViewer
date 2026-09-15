"""poll progress phase and cancellation

Revision ID: a1c5e7d29f04
Revises: e4a7c1b95d82
Create Date: 2026-09-15

Two columns the running session needs.

`progress_phase` because a count without a unit is a lie: the bar said
"прочитано годин" while the daily archive was being read, and the numbers
under it were days. The agent knows which ring it is walking, so it says so.

`cancel_requested_at` because a call that was started by mistake had no way
to end but waiting it out — up to twenty minutes of a modem nobody wanted
busy. The agent sees the flag on its next log push, a second or two later,
and hangs up between records.
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c5e7d29f04"
down_revision = "e4a7c1b95d82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "poll_device",
        sa.Column("progress_phase", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "poll_device",
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("poll_device", "cancel_requested_at")
    op.drop_column("poll_device", "progress_phase")
