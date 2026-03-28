"""drop dpd_global_config table

Revision ID: 6a7b8c9d0e1f
Revises: 528d95ce220f
Create Date: 2026-03-29

"""
import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = '6a7b8c9d0e1f'
down_revision = '528d95ce220f'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('dpd_global_config')


def downgrade():
    op.create_table(
        'dpd_global_config',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('api_base_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('auth_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('timeout_sec', sa.Integer(), nullable=False, server_default=sa.text('30')),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
