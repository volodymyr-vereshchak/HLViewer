"""DPD lines can be included in the 24h report / Overview tab.

Adds dpd_line.include_in_report — same flag physical lines carry
(gas_volume_line.include_in_report, migration 6f7a8b9c0d1e).

Revision ID: e9f0a1b2c3d4
Revises: c6d7e8f9a0b1
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dpd_line",
        sa.Column("include_in_report", sa.Boolean(), nullable=False,
                  server_default="false"),
    )
    op.create_index("ix_dpd_line_include_in_report", "dpd_line",
                    ["include_in_report"])


def downgrade() -> None:
    op.drop_index("ix_dpd_line_include_in_report", table_name="dpd_line")
    op.drop_column("dpd_line", "include_in_report")
