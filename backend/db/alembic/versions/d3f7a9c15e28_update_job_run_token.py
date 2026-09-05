"""Give the update job a run token so a run can prove the lock is still its own

status = 'running' cannot distinguish a live run from the hung one whose lock
it took over — both are running — so a process that woke up after being
replaced could close the run that replaced it. acquire() now stamps a token
and heartbeat/finalize match on it.

Nullable, with no backfill: a job in flight during the deploy has no token, and
the guards treat a NULL token as "do not check", which is the behaviour that
run had when it started.

Revision ID: d3f7a9c15e28
Revises: c8b1d4e7f209
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "d3f7a9c15e28"
down_revision = "c8b1d4e7f209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "update_job",
        sa.Column("run_token", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("update_job", "run_token")
