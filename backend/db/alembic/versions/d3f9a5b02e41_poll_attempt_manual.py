"""Mark which poll attempts a person asked for

A failed scheduled slot is retried at most three times. Counting a manual poll
among them meant that looking at a site — the thing an operator does precisely
when it is failing — silently spent the automatic calls it still had.

Revision ID: d3f9a5b02e41
Revises: c8e2f47a91d5
"""
import sqlalchemy as sa
from alembic import op

revision = "d3f9a5b02e41"
down_revision = "c8e2f47a91d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows become "scheduled": the column did not exist when they
    # were written, and the schedule is what most of them were.
    op.add_column(
        "poll_attempt",
        sa.Column("manual", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("poll_attempt", "manual")
