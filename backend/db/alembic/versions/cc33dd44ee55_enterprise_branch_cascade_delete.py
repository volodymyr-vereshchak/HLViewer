"""enterprise branch_id FK: SET NULL -> CASCADE

Revision ID: cc33dd44ee55
Revises: bb22cc33dd44
Create Date: 2026-03-23
"""
import sqlalchemy as sa
from alembic import op

revision = 'cc33dd44ee55'
down_revision = 'bb22cc33dd44'
branch_labels = None
depends_on = None


def upgrade():
    # Drop old FK (SET NULL), recreate as CASCADE
    op.drop_constraint('fk_enterprise_branch_id', 'enterprise', type_='foreignkey')
    op.create_foreign_key(
        'fk_enterprise_branch_id',
        'enterprise', 'grmu_branch',
        ['branch_id'], ['id'],
        ondelete='CASCADE',
    )


def downgrade():
    op.drop_constraint('fk_enterprise_branch_id', 'enterprise', type_='foreignkey')
    op.create_foreign_key(
        'fk_enterprise_branch_id',
        'enterprise', 'grmu_branch',
        ['branch_id'], ['id'],
        ondelete='SET NULL',
    )
