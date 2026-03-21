"""drop include_in_report and is_high_pressure from virtual_line

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-03-21

"""
import sqlalchemy as sa
from alembic import op

revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_virtual_line_include_in_report", table_name="virtual_line")
    op.drop_column("virtual_line", "include_in_report")
    op.drop_column("virtual_line", "is_high_pressure")


def downgrade() -> None:
    op.add_column("virtual_line", sa.Column("is_high_pressure", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("virtual_line", sa.Column("include_in_report", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("idx_virtual_line_include_in_report", "virtual_line", ["include_in_report"])
