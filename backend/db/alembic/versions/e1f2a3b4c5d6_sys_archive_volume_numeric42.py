"""sys_archive volume NUMERIC(20,3) -> DOUBLE PRECISION to match other archives and cover full float32 range

Revision ID: e1f2a3b4c5d6
Revises: 6a7b8c9d0e1f
Create Date: 2026-05-05

"""
import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "6a7b8c9d0e1f"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "sys_archive",
        "volume",
        type_=sa.Float(),
        existing_type=sa.Numeric(precision=20, scale=3),
        existing_nullable=False,
    )


def downgrade():
    op.alter_column(
        "sys_archive",
        "volume",
        type_=sa.Numeric(precision=20, scale=3),
        existing_type=sa.Float(),
        existing_nullable=False,
    )
