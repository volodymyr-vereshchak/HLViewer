"""Admin-configurable DPD refresh schedule.

Adds dpd_refresh_job.refresh_times — comma-separated local times (HH:MM) at
which the scheduler re-polls the DPD archive. NULL means "not configured in the
admin panel": the scheduler then falls back to DPD_REFRESH_TIMES from the
environment, so an untouched deployment keeps its current schedule.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-07-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dpd_refresh_job",
        sa.Column("refresh_times", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dpd_refresh_job", "refresh_times")
