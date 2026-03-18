"""add app_user and app_user_branch_access tables

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-03-17 00:00:07.000000

Adds app_user (Windows Auth user registry) and app_user_branch_access
(per-user branch access control, used in Phase 2 RBAC).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7a8b9c0d1e2f'
down_revision = '6f7a8b9c0d1e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'app_user',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('windows_username', sa.String(255), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=True),
        sa.Column('role', sa.String(32), nullable=False, server_default='viewer'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('windows_username', name='uq_app_user_username'),
    )
    op.create_index('idx_app_user_username', 'app_user', ['windows_username'])
    op.create_index('idx_app_user_role', 'app_user', ['role'])
    op.create_index('idx_app_user_active', 'app_user', ['active'])

    op.create_table(
        'app_user_branch_access',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('app_user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('branch_id', sa.Integer(), sa.ForeignKey('grmu_branch.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'branch_id', name='uq_app_user_branch_access'),
    )


def downgrade():
    op.drop_table('app_user_branch_access')
    op.drop_index('idx_app_user_active', table_name='app_user')
    op.drop_index('idx_app_user_role', table_name='app_user')
    op.drop_index('idx_app_user_username', table_name='app_user')
    op.drop_table('app_user')
