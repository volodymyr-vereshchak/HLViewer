"""add branch_id to enterprise

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-21

"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'enterprise',
        sa.Column('branch_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'fk_enterprise_branch_id',
        'enterprise', 'grmu_branch',
        ['branch_id'], ['id'],
        ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_enterprise_branch_id', 'enterprise', type_='foreignkey')
    op.drop_column('enterprise', 'branch_id')
