"""add dpd url fields to branch credential

Revision ID: a1b2c3d4e5f6
Revises: f7a8b9c0d1e2
Create Date: 2026-03-28

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('grmu_branch_dpd_credential',
        sa.Column('api_base_url', sa.String(512), nullable=True))
    op.add_column('grmu_branch_dpd_credential',
        sa.Column('auth_url', sa.String(512), nullable=True))
    op.add_column('grmu_branch_dpd_credential',
        sa.Column('timeout_sec', sa.Integer(), nullable=False, server_default='30'))


def downgrade() -> None:
    op.drop_column('grmu_branch_dpd_credential', 'timeout_sec')
    op.drop_column('grmu_branch_dpd_credential', 'auth_url')
    op.drop_column('grmu_branch_dpd_credential', 'api_base_url')
