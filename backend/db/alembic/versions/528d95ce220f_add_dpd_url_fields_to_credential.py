"""add_dpd_url_fields_to_credential

Revision ID: 528d95ce220f
Revises: cc33dd44ee55
Create Date: 2026-03-28 23:31:27.168894

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel             # NEW
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '528d95ce220f'
down_revision = 'cc33dd44ee55'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('grmu_branch_dpd_credential', sa.Column('api_base_url', sa.String(length=512), nullable=True))
    op.add_column('grmu_branch_dpd_credential', sa.Column('auth_url', sa.String(length=512), nullable=True))
    op.add_column('grmu_branch_dpd_credential', sa.Column('timeout_sec', sa.Integer(), nullable=False, server_default='30'))


def downgrade():
    op.drop_column('grmu_branch_dpd_credential', 'timeout_sec')
    op.drop_column('grmu_branch_dpd_credential', 'auth_url')
    op.drop_column('grmu_branch_dpd_credential', 'api_base_url')
