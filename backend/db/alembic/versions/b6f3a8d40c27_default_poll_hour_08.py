"""default polling hour 06:00 -> 08:00

Revision ID: b6f3a8d40c27
Revises: a1c5e7d29f04
Create Date: 2026-09-15

The hour a site is polled at when nobody sets one. Moved on request: an
archive read at 06:00 is read before the working day, and a failure has
nobody to notice it for two hours.

The stored row is moved with the default, but only if it is still the old
default — a schedule somebody has actually chosen is theirs, even where they
chose the same hour we happened to ship.
"""
from alembic import op

revision = "b6f3a8d40c27"
down_revision = "a1c5e7d29f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE poll_settings ALTER COLUMN poll_times SET DEFAULT '[\"08:00\"]'"
    )
    op.execute(
        "UPDATE poll_settings SET poll_times = '[\"08:00\"]' "
        "WHERE poll_times = '[\"06:00\"]'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE poll_settings ALTER COLUMN poll_times SET DEFAULT '[\"06:00\"]'"
    )
    op.execute(
        "UPDATE poll_settings SET poll_times = '[\"06:00\"]' "
        "WHERE poll_times = '[\"08:00\"]'"
    )
