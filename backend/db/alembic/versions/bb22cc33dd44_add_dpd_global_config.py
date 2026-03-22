"""add dpd_global_config and simplify dpd_credential

Revision ID: bb22cc33dd44
Revises: aa11bb22cc33
Create Date: 2026-03-22
"""
import sqlalchemy as sa
import sqlmodel
from alembic import op


revision = 'bb22cc33dd44'
down_revision = 'aa11bb22cc33'
branch_labels = None
depends_on = None


def upgrade():
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

    op.drop_column('grmu_branch_dpd_credential', 'api_base_url')
    op.drop_column('grmu_branch_dpd_credential', 'auth_url')
    op.drop_column('grmu_branch_dpd_credential', 'timeout_sec')


def downgrade():
    op.add_column('grmu_branch_dpd_credential', sa.Column('timeout_sec', sa.Integer(), nullable=False, server_default=sa.text('30')))
    op.add_column('grmu_branch_dpd_credential', sa.Column('auth_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
    op.add_column('grmu_branch_dpd_credential', sa.Column('api_base_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''))
    op.drop_table('dpd_global_config')
