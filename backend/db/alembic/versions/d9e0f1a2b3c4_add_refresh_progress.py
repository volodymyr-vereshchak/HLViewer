"""Add per-device progress counters to dpd_refresh_job.

progress_done / progress_total let the admin panel show a progress bar for
a running archive refresh (each device is polled twice: daily + hourly).
Both are NULL outside a running refresh.

Revision ID: d9e0f1a2b3c4
Revises: c8d4e2f1a3b5
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, None] = "c8d4e2f1a3b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dpd_refresh_job", sa.Column("progress_done", sa.Integer(), nullable=True)
    )
    op.add_column(
        "dpd_refresh_job", sa.Column("progress_total", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("dpd_refresh_job", "progress_total")
    op.drop_column("dpd_refresh_job", "progress_done")
