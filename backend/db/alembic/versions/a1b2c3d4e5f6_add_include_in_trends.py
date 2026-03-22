"""add include_in_trends flag to gas_volume_line and virtual_line

Revision ID: a1b2c3d4e5f6
Revises: f7a8b9c0d1e2
Create Date: 2026-03-22
"""
import sqlalchemy as sa
from alembic import op

revision = 'aa11bb22cc33'
down_revision = 'b0c1d2e3f4a5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'gas_volume_line',
        sa.Column('include_in_trends', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index('idx_line_include_in_trends', 'gas_volume_line', ['include_in_trends'])

    op.add_column(
        'virtual_line',
        sa.Column('include_in_trends', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index('idx_virtual_line_include_in_trends', 'virtual_line', ['include_in_trends'])


def downgrade() -> None:
    op.drop_index('idx_virtual_line_include_in_trends', table_name='virtual_line')
    op.drop_column('virtual_line', 'include_in_trends')
    op.drop_index('idx_line_include_in_trends', table_name='gas_volume_line')
    op.drop_column('gas_volume_line', 'include_in_trends')
